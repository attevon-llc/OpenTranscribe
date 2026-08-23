"""``GET /api/search``'s ``total_pages`` for the summaries-only leg.

A ``result_type=summaries`` request never runs the transcript leg, so the
placeholder response built for that case used to leave `total_pages`
hardcoded at 0 no matter how many summary hits were actually found —
real pagination never reached the client, and the frontend had to work around it.
Kept as its own small file (rather than folded into the larger
``test_search_result_type.py``) so it stays a narrow, obviously-scoped pin on the
pagination fix alone.
"""

from __future__ import annotations

import uuid as uuid_pkg

from app.services.search.hybrid_search_service import HybridSearchService
from app.services.search.hybrid_search_service import SearchResponse

SEARCH_PATH = "/api/search"


def _explode(_self, **_kwargs):
    raise AssertionError("HybridSearchService.search was called for a non-transcript-only request")


def test_summaries_only_total_pages_reflects_the_summary_total(
    client, user_token_headers, monkeypatch
):
    monkeypatch.setattr(HybridSearchService, "search", _explode)

    from app.services.search import summary_search as summary_search_mod

    class _FakeSummaryHit:
        def __init__(self, file_uuid, file_id, title):
            self.file_uuid = file_uuid
            self.file_id = file_id
            self.title = title
            self.matches = []

    class _FakeSummaryResult:
        # Real UUID strings: the endpoint's non-admin quarantine filter queries
        # MediaFile.uuid.in_(...) against these, and Postgres rejects a bare
        # non-UUID string for a `uuid` column.
        results = [_FakeSummaryHit(str(uuid_pkg.uuid4()), i, f"file-{i}.txt") for i in range(5)]
        total = 5

    monkeypatch.setattr(
        summary_search_mod, "search_summaries", lambda *a, **k: _FakeSummaryResult()
    )

    body = client.get(
        SEARCH_PATH,
        params={"q": "distinctive", "result_type": "summaries", "page_size": 2},
        headers=user_token_headers,
    ).json()

    assert body["summary_total"] == 5
    # 5 summaries at page_size=2 is 3 pages — the value the client actually needs
    # to render pagination controls, not the hardcoded 0 every summaries-only
    # request used to carry regardless of how many hits were found.
    assert body["total_pages"] == 3


def test_summaries_only_total_pages_is_zero_when_nothing_matched(
    client, user_token_headers, monkeypatch
):
    monkeypatch.setattr(HybridSearchService, "search", _explode)

    from app.services.search import summary_search as summary_search_mod

    class _EmptyResult:
        results = []
        total = 0

    monkeypatch.setattr(summary_search_mod, "search_summaries", lambda *a, **k: _EmptyResult())

    body = client.get(
        SEARCH_PATH,
        params={"q": "nothing-matches-this", "result_type": "summaries"},
        headers=user_token_headers,
    ).json()

    assert body["summary_total"] == 0
    assert body["total_pages"] == 0


def test_transcripts_result_type_total_pages_is_the_transcript_legs_own_value(
    client, user_token_headers, monkeypatch
):
    """The summaries pagination fix must not fire for an ordinary
    transcript search — its own `total_pages` (computed by `HybridSearchService`)
    must reach the client unrecomputed.
    """

    def _fake_search(_self, **kwargs):
        return SearchResponse(
            query=kwargs.get("query", ""),
            results=[],
            total_results=41,
            total_files=41,
            page=1,
            page_size=20,
            # Deliberately NOT ceil(41/20): if the endpoint were (wrongly) recomputing
            # this instead of passing the transcript service's own value through, this
            # test would see 3, not 99, and fail.
            total_pages=99,
            search_time_ms=1.0,
        )

    monkeypatch.setattr(HybridSearchService, "search", _fake_search)

    body = client.get(
        SEARCH_PATH,
        params={"q": "q", "result_type": "transcripts"},
        headers=user_token_headers,
    ).json()

    assert body["total_pages"] == 99
    assert "summary_total" not in body
