"""Every matching file is reachable across pages (#1078, #1079).

* #1078: a keyword search with a non-relevance sort was cut to one page server-side
  (``from``/``size=page_size``) and then paged again client-side, so page 2 was always
  empty and ``total_pages`` was always 1.
* #1079: hybrid only backfilled keyword-matching files the fused window starved when
  it returned less than ONE page of files, so any file past the ~200-chunk window was
  unreachable once the window held more than a page.

The real-engine side is ``tests/integration/test_search_completeness_opensearch.py``.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.services.search import hybrid_search_service as hss

pytestmark = [pytest.mark.unit]

PAGE_SIZE = 10
BM25_FILES = 35
HYBRID_FILES = 12  # more than a page: the case the old backfill condition skipped


def _uuid(i: int) -> str:
    return f"file-{i:03d}"


def _source(i: int) -> dict[str, Any]:
    return {
        "file_uuid": _uuid(i),
        "title": f"t{i}",
        "language": "en",
        "upload_time": f"2026-01-{i + 1:02d}T00:00:00Z",
    }


class _FakeOpenSearch:
    """Answers the hybrid collapse, the BM25 collapse, its segments agg and highlights."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def count(self, index=None, body=None):  # noqa: ARG002
        return {"count": BM25_FILES}

    def search(self, index=None, body=None, params=None):  # noqa: ARG002
        body = body or {}
        self.bodies.append(body)
        if "hybrid" in body.get("query", {}):
            hits = [
                {
                    "_score": 1.0 - i * 0.01,
                    "_source": _source(i),
                    "inner_hits": {
                        "segments": {
                            "hits": {
                                "total": {"value": 1},
                                "hits": [
                                    {
                                        "_id": f"{i}_0",
                                        "_score": 1.0 - i * 0.01,
                                        "_source": {"content": f"budget {i}", "chunk_index": 0},
                                        "highlight": {"content": [f"<mark>budget</mark> {i}"]},
                                    }
                                ],
                            }
                        }
                    },
                }
                for i in range(HYBRID_FILES)
            ]
            return {"hits": {"hits": hits}}
        if "collapse" in body:
            order = list(range(BM25_FILES))
            if body.get("sort"):
                order.reverse()  # "upload_time desc": the highest index is newest
            hits = [{"_score": 10.0 - i * 0.1, "_source": _source(i)} for i in order]
            return {
                "hits": {"hits": hits[: body["size"]]},
                "aggregations": {"total_files": {"value": BM25_FILES}},
            }
        if "files" in body.get("aggs", {}):
            uuids = next(
                f["terms"]["file_uuid"]
                for f in body["query"]["bool"]["filter"]
                if "file_uuid" in f.get("terms", {})
            )
            buckets = [
                {
                    "key": u,
                    "doc_count": 1,
                    "segments": {
                        "hits": {
                            "hits": [
                                {
                                    "_id": f"{u}_0",
                                    "_score": 5.0,
                                    "_source": {"content": f"budget in {u}", "chunk_index": 0},
                                }
                            ]
                        }
                    },
                }
                for u in uuids
            ]
            return {"hits": {"hits": []}, "aggregations": {"files": {"buckets": buckets}}}
        if "highlight" in body:
            ids = next(f["ids"]["values"] for f in body["query"]["bool"]["filter"] if "ids" in f)
            return {
                "hits": {
                    "hits": [
                        {"_id": i, "highlight": {"content.exact": ["<mark>budget</mark>"]}}
                        for i in ids
                    ]
                }
            }
        raise AssertionError(f"unexpected request body: {body}")


@pytest.fixture
def fake(monkeypatch) -> _FakeOpenSearch:
    client = _FakeOpenSearch()
    monkeypatch.setattr(hss, "get_opensearch_client", lambda: client)
    monkeypatch.setattr(hss.HybridSearchService, "_get_neural_model_id", lambda self: "m")
    return client


def _search(page: int, sort_by: str = "relevance", neural: bool = False) -> hss.SearchResponse:
    return hss.HybridSearchService()._search_with_collapse(
        query="budget",
        search_query="budget",
        filters=[{"terms": {"accessible_user_ids": [7]}}],
        page=page,
        page_size=PAGE_SIZE,
        sort_by=sort_by,
        sort_order="desc",
        search_mode="hybrid" if neural else "keyword",
        filters_applied={},
        start_time=time.time(),
        has_speaker_filter=False,
        use_neural=neural,
        search_pipeline="p",
    )


def _all_pages(**kw) -> list[hss.SearchResponse]:
    first = _search(1, **kw)
    return [first] + [_search(n, **kw) for n in range(2, first.total_pages + 1)]


def test_a_sorted_keyword_search_reaches_every_page(fake):
    pages = _all_pages(sort_by="upload_time")

    seen = [h.file_uuid for p in pages for h in p.results]
    assert len(pages) == 4
    assert seen == [_uuid(i) for i in reversed(range(BM25_FILES))]
    assert {p.total_files for p in pages} == {BM25_FILES}


def test_a_sorted_keyword_body_is_not_cut_to_one_page_server_side(fake):
    _search(2, sort_by="upload_time")

    collapse = next(b for b in fake.bodies if "collapse" in b)
    assert "from" not in collapse
    assert collapse["size"] > PAGE_SIZE


def test_hybrid_reaches_keyword_files_even_when_its_window_fills_a_page(fake):
    pages = _all_pages(neural=True)

    seen = [h.file_uuid for p in pages for h in p.results]
    assert len(seen) == len(set(seen)), "a file appeared on two pages"
    assert set(seen) == {_uuid(i) for i in range(BM25_FILES)}
    assert seen[:HYBRID_FILES] == [_uuid(i) for i in range(HYBRID_FILES)], (
        "hybrid-ranked files must keep their positions ahead of the backfill"
    )


def test_backfilled_files_on_a_page_are_highlighted(fake):
    page = _search(3, neural=True)

    assert page.results
    assert all(h.file_uuid not in {_uuid(i) for i in range(HYBRID_FILES)} for h in page.results)
    for hit in page.results:
        assert all("<mark>budget</mark>" in o.snippet for o in hit.occurrences)


def test_the_fused_window_does_not_grow_with_the_page(fake):
    """A window that grew on deep pages pulled backfilled files into the head."""
    _search(1, neural=True)
    _search(9, neural=True)

    sizes = [b["size"] for b in fake.bodies if "hybrid" in b.get("query", {})]
    assert len(sizes) == 2
    assert sizes[0] == sizes[1]
