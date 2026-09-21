"""GET /api/search's ``sources`` parameter (issue #760).

Multi-select result sources — content, title, speaker, summary — replacing
the exclusive two-value ``result_type`` tab on the search page. ``sources``
wins over ``result_type`` when both are supplied; omitting it entirely keeps
the legacy ``result_type`` behavior byte-identical (pinned in
``test_search_result_type.py``, unedited by this file).

**OR-union (J-760-10) is the headline property under test here**: a file
matching via EITHER the transcript leg OR the summary leg must appear when
both sources are selected — never AND-intersected. The transcript and summary
legs are independent OpenSearch queries (`HybridSearchService.search` /
`HybridSearchService.search_summary_plane`), both stubbed here so this file
needs no live OpenSearch. Field-level OR-union (content/title/speaker as one
`multi_match`) is pinned at the unit level in `test_search_source_scoping.py`.
"""

from __future__ import annotations

from app.services.search.hybrid_search_service import HybridSearchService
from app.services.search.hybrid_search_service import SearchHit
from app.services.search.hybrid_search_service import SearchResponse

SEARCH_PATH = "/api/search"


def _transcript_response(query: str, file_uuid: str | None = None) -> SearchResponse:
    results = []
    if file_uuid:
        results.append(
            SearchHit(
                file_uuid=file_uuid,
                file_id=1,
                title="a transcript hit",
                speakers=[],
                tags=[],
                upload_time="2026-03-10T00:00:00+00:00",
                language="en",
            )
        )
    return SearchResponse(
        query=query,
        results=results,
        total_results=len(results),
        total_files=len(results),
        page=1,
        page_size=20,
        total_pages=1 if results else 0,
        search_time_ms=1.0,
    )


def _install_transcript_stub(
    monkeypatch, file_uuid: str | None = None, captured: dict | None = None
):
    def _fake_search(_self, **kwargs):
        if captured is not None:
            captured["sources"] = kwargs.get("sources")
        return _transcript_response(kwargs.get("query", ""), file_uuid)

    monkeypatch.setattr(HybridSearchService, "search", _fake_search)


def _install_summary_stub(monkeypatch, file_uuid: str | None = None):
    def _fake_search_summary_plane(_self, *_args, **_kwargs):
        if not file_uuid:
            return [], 0
        return (
            [
                {
                    "file_uuid": file_uuid,
                    "file_id": 2,
                    "title": "a summary hit",
                    "matches": [{"key_path": "bluf", "content": "match", "score": 1.0}],
                }
            ],
            1,
        )

    monkeypatch.setattr(HybridSearchService, "search_summary_plane", _fake_search_summary_plane)


def _install_count_stub(monkeypatch):
    def _fake_count_matches(_self, *_args, **kwargs):
        return 0

    monkeypatch.setattr(HybridSearchService, "count_matches", _fake_count_matches)


class TestSourcesParameterValidation:
    def test_an_unknown_source_is_400(self, client, user_token_headers):
        response = client.get(
            SEARCH_PATH,
            params={"q": "test", "sources": ["bogus"]},
            headers=user_token_headers,
        )
        assert response.status_code == 400, response.text
        assert "sources" in response.json()["detail"]

    def test_an_explicitly_empty_sources_value_is_400(self, client, user_token_headers):
        """An empty string is not a member of SEARCH_SOURCES — the endpoint
        cannot distinguish 'omitted' from 'sent empty' over a repeated query
        param, so this is how J-760-8's wire-side 400 is actually reached."""
        response = client.get(
            SEARCH_PATH,
            params={"q": "test", "sources": [""]},
            headers=user_token_headers,
        )
        assert response.status_code == 400, response.text

    def test_sources_wins_over_result_type_when_both_are_supplied(
        self, client, user_token_headers, monkeypatch
    ):
        captured: dict = {}
        _install_transcript_stub(monkeypatch, captured=captured)
        _install_summary_stub(monkeypatch)
        response = client.get(
            SEARCH_PATH,
            params={"q": "test", "result_type": "summaries", "sources": ["content"]},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text
        # result_type=summaries would have skipped the transcript leg entirely
        # (want_transcripts=False) — sources=content means it must have run.
        assert captured["sources"] == frozenset({"content"})


class TestOmittingSourcesIsByteIdenticalToBefore:
    def test_no_sources_or_source_counts_keys_when_sources_is_omitted(
        self, client, user_token_headers, monkeypatch
    ):
        _install_transcript_stub(monkeypatch)
        body = client.get(SEARCH_PATH, params={"q": "test"}, headers=user_token_headers).json()
        assert "sources" not in body
        assert "source_counts" not in body


class TestOrUnionAcrossTranscriptAndSummaryLegs:
    """J-760-10: a file matches if it matches ANY selected source."""

    def test_both_sources_selected_returns_the_union_not_the_intersection(
        self, client, user_token_headers, monkeypatch
    ):
        transcript_only_uuid = "11111111-1111-1111-1111-111111111111"
        summary_only_uuid = "22222222-2222-2222-2222-222222222222"
        _install_transcript_stub(monkeypatch, file_uuid=transcript_only_uuid)
        _install_summary_stub(monkeypatch, file_uuid=summary_only_uuid)

        response = client.get(
            SEARCH_PATH,
            params={"q": "test", "sources": ["content", "summary"]},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()

        transcript_uuids = {hit["file_uuid"] for hit in body["results"]}
        summary_uuids = {hit["file_uuid"] for hit in body["summary_results"]}

        # Neither file matched BOTH legs (an AND-intersect would return
        # neither); the OR-union returns both, one per section.
        assert transcript_uuids == {transcript_only_uuid}
        assert summary_uuids == {summary_only_uuid}

    def test_summary_only_source_never_calls_the_transcript_leg(
        self, client, user_token_headers, monkeypatch
    ):
        def _explode(_self, **_kwargs):
            raise AssertionError("transcript leg must not run for sources=summary")

        monkeypatch.setattr(HybridSearchService, "search", _explode)
        _install_summary_stub(monkeypatch, file_uuid="33333333-3333-3333-3333-333333333333")

        response = client.get(
            SEARCH_PATH,
            params={"q": "test", "sources": ["summary"]},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["results"] == []


class TestSourceCounts:
    def test_source_counts_present_only_when_sources_is_explicit(
        self, client, user_token_headers, monkeypatch
    ):
        _install_transcript_stub(monkeypatch)
        _install_summary_stub(monkeypatch)
        _install_count_stub(monkeypatch)

        body = client.get(
            SEARCH_PATH,
            params={"q": "test", "sources": ["content", "title", "speaker", "summary"]},
            headers=user_token_headers,
        ).json()

        assert body["sources"] == ["content", "speaker", "summary", "title"]
        assert set(body["source_counts"].keys()) == {"content", "title", "speaker", "summary"}

    def test_a_failed_count_is_null_never_zero(self, client, user_token_headers, monkeypatch):
        from app.services.search.hybrid_search_service import SearchCountUnavailableError

        _install_transcript_stub(monkeypatch)
        _install_summary_stub(monkeypatch)

        def _raise(_self, *_args, **_kwargs):
            raise SearchCountUnavailableError("boom")

        monkeypatch.setattr(HybridSearchService, "count_matches", _raise)

        body = client.get(
            SEARCH_PATH,
            params={"q": "test", "sources": ["content"]},
            headers=user_token_headers,
        ).json()

        assert body["source_counts"]["content"] is None
