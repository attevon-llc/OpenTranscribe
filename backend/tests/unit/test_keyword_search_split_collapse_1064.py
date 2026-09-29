"""The keyword (BM25-only) search path never asks OpenSearch for per-group work (#1064).

``collapse.inner_hits`` runs one sub-search per collapsed group, and each sub-search
re-rewrites the fuzzy clauses and re-builds the highlighter. Multiplied by the relevance
over-fetch (200 groups at page 1), that made keyword search 3-10x slower than neural
search. The keyword path now sends three flat requests; these tests pin the shape of
what reaches the wire, which is what the latency depends on:

* no request carries ``collapse.inner_hits``;
* exactly one request asks for highlights, and only for the displayed page's segments;
* the page still renders the highlights, and the over-fetched groups still count.

The wall-clock side, against a real engine, is
``tests/integration/test_keyword_search_latency_opensearch.py``.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.core.constants import SEARCH_MAX_SNIPPETS_PER_FILE
from app.services.search import hybrid_search_service as hss

pytestmark = [pytest.mark.unit]

N_FILES = 30
SEGS_PER_FILE = 3
PAGE_SIZE = 10


def _file_uuid(i: int) -> str:
    return f"file-{i:03d}"


class _FakeOpenSearch:
    """Answers the collapse, the segments agg and the page highlight like OpenSearch."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def search(self, index=None, body=None, params=None):  # noqa: ARG002
        body = body or {}
        self.bodies.append(body)
        if "collapse" in body:
            hits = []
            for i in range(N_FILES):
                hit: dict[str, Any] = {
                    "_id": f"{i}_0",
                    "_score": float(N_FILES - i),
                    "_source": {"file_uuid": _file_uuid(i), "title": f"t{i}", "language": "en"},
                }
                if "inner_hits" in body["collapse"]:
                    hit["inner_hits"] = {
                        "segments": {"hits": {"total": {"value": SEGS_PER_FILE}, "hits": []}}
                    }
                hits.append(hit)
            return {"hits": {"hits": hits}, "aggregations": {"total_files": {"value": N_FILES}}}
        if "files" in body.get("aggs", {}):
            uuids = next(
                f["terms"]["file_uuid"]
                for f in body["query"]["bool"]["filter"]
                if "terms" in f and "file_uuid" in f["terms"]
            )
            buckets = []
            for u in uuids:
                i = int(u.split("-")[1])
                segs = [
                    {
                        "_id": f"{i}_{j}",
                        "_score": float(N_FILES - i) - j * 0.1,
                        "_source": {
                            "file_uuid": u,
                            "content": f"the budget for file {i} segment {j}",
                            "chunk_index": j,
                            "speaker": "SPEAKER_00",
                        },
                    }
                    for j in range(SEGS_PER_FILE)
                ]
                buckets.append(
                    {"key": u, "doc_count": SEGS_PER_FILE, "segments": {"hits": {"hits": segs}}}
                )
            return {"hits": {"hits": []}, "aggregations": {"files": {"buckets": buckets}}}
        if "highlight" in body:
            ids = next(f["ids"]["values"] for f in body["query"]["bool"]["filter"] if "ids" in f)
            return {
                "hits": {
                    "hits": [
                        {"_id": i, "highlight": {"content.exact": [f"the <mark>budget</mark> {i}"]}}
                        for i in ids
                    ]
                }
            }
        raise AssertionError(f"unexpected request body: {body}")


@pytest.fixture
def fake(monkeypatch) -> _FakeOpenSearch:
    client = _FakeOpenSearch()
    monkeypatch.setattr(hss, "get_opensearch_client", lambda: client)
    return client


def _keyword_search(query: str = "budget", page: int = 1) -> hss.SearchResponse:
    return hss.HybridSearchService()._search_with_collapse(
        query=query,
        search_query=query,
        filters=[{"terms": {"accessible_user_ids": [7]}}],
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


def _has_inner_hits(body: dict[str, Any]) -> bool:
    return "inner_hits" in body.get("collapse", {})


def test_no_request_asks_for_per_group_inner_hits(fake):
    _keyword_search()

    assert fake.bodies, "no request reached the client"
    assert not [b for b in fake.bodies if _has_inner_hits(b)]


def test_only_the_displayed_pages_segments_are_highlighted(fake):
    result = _keyword_search(page=2)

    highlighted = [b for b in fake.bodies if "highlight" in b]
    assert len(highlighted) == 1
    ids = next(f["ids"]["values"] for f in highlighted[0]["query"]["bool"]["filter"] if "ids" in f)
    page_files = [h.file_uuid for h in result.results]
    assert page_files == [_file_uuid(i) for i in range(PAGE_SIZE, 2 * PAGE_SIZE)]
    assert sorted(ids) == sorted(
        f"{int(u.split('-')[1])}_{j}" for u in page_files for j in range(SEGS_PER_FILE)
    )
    assert len(ids) <= PAGE_SIZE * SEARCH_MAX_SNIPPETS_PER_FILE


def test_the_highlight_request_keeps_the_scoring_query_and_filters(fake):
    _keyword_search()

    collapse = next(b for b in fake.bodies if "collapse" in b)
    highlight = next(b for b in fake.bodies if "highlight" in b)
    assert highlight["query"]["bool"]["must"] == collapse["query"]["bool"]["must"]
    assert collapse["query"]["bool"]["filter"] == highlight["query"]["bool"]["filter"][:-1]


def test_the_page_renders_highlights_and_every_group_still_counts(fake):
    result = _keyword_search()

    assert result.total_files == N_FILES
    assert result.total_results == N_FILES * SEGS_PER_FILE
    for hit in result.results:
        assert len(hit.occurrences) == SEGS_PER_FILE
        assert all("<mark>budget</mark>" in o.snippet for o in hit.occurrences)
        assert all(o.has_keyword_match for o in hit.occurrences)
        assert "content" in hit.match_sources
        assert not hit.semantic_only
        scores = [o.score for o in hit.occurrences]
        assert scores == sorted(scores, reverse=True)


def test_an_empty_query_is_not_split(fake):
    """A browse is ``match_all``: its hits are not keyword matches, so no assumption."""
    _keyword_search(query="")

    assert len(fake.bodies) == 1
    assert _has_inner_hits(fake.bodies[0])
