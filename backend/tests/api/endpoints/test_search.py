"""Search endpoint tests.

**What was wrong with this file before.** ``test_search_with_filters`` sent
``params={"q": "test", "mode": "keyword"}`` — but the route's parameter is
``search_mode``. FastAPI ignores an unknown query key, so that request was
byte-identical to ``test_search_files``: the test had never exercised a filter or
a search mode in its life, and would have stayed green if every filter had been
deleted from the handler. ``test_search_files`` also asserted ``"results" in data
or "items" in data``, where the ``"items"`` branch is dead — the endpoint has only
ever emitted ``results``.

11 of the 19 query parameters were never varied by any test, and
``_drop_quarantined_search_hits`` — the DMCA/abuse takedown gate applied to search
RESULTS — had no HTTP test at all.

Most of what follows needs no OpenSearch: parameter validation happens before the
search service is constructed, and the takedown gate is a DB query over the hits
the service returned, so a recorded/stubbed service exercises the real endpoint
code. Only the two live round trips at the bottom are gated on the index being
up — which is deliberate: gating the whole module is why nothing here ran in CI.
"""

from __future__ import annotations

import os
import uuid as uuid_pkg

import pytest

from app.models.media import MediaFile
from app.services.search.hybrid_search_service import HybridSearchService
from app.services.search.hybrid_search_service import SearchHit
from app.services.search.hybrid_search_service import SearchResponse

requires_opensearch = pytest.mark.skipif(
    os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true",
    reason="OpenSearch is disabled in this environment",
)

SEARCH_PATH = "/api/search"


def _hit(file_uuid: str, file_id: int) -> SearchHit:
    return SearchHit(
        file_uuid=str(file_uuid),
        file_id=file_id,
        title=f"file-{file_id}",
        speakers=[],
        tags=[],
        upload_time="2026-01-01T00:00:00",
        language="en",
    )


def _response(hits: list[SearchHit], query: str = "test") -> SearchResponse:
    return SearchResponse(
        query=query,
        results=hits,
        total_results=len(hits),
        total_files=len(hits),
        page=1,
        page_size=20,
        total_pages=1,
        search_time_ms=1.0,
    )


def _make_file(db_session, user, *, quarantined: bool) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        filename=f"{file_uuid}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        user_id=user.id,
        status="completed",
        is_quarantined=quarantined,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


@pytest.fixture
def search_recorder(monkeypatch) -> list[dict]:
    """Record the arguments the endpoint hands the search service.

    The point is coverage of the *wiring*: a filter that the handler accepts and
    then forgets to forward is invisible to an assertion on the response, because
    the response of a filtered search over an empty index looks exactly like the
    response of an unfiltered one.
    """
    calls: list[dict] = []

    def _fake_search(_self, **kwargs):
        calls.append(kwargs)
        return _response([], query=kwargs.get("query", ""))

    monkeypatch.setattr(HybridSearchService, "search", _fake_search)
    return calls


@pytest.fixture
def stub_search(monkeypatch):
    """Return a callable that pins the search service's response for one test."""

    def _install(hits: list[SearchHit]) -> None:
        def _fake_search(_self, **kwargs):
            return _response(hits, query=kwargs.get("query", ""))

        monkeypatch.setattr(HybridSearchService, "search", _fake_search)

    return _install


# --------------------------------------------------------------------------- #
# Parameter contract                                                           #
# --------------------------------------------------------------------------- #
class TestSearchParameterContract:
    def test_the_search_mode_parameter_is_named_search_mode(self):
        """Pins the name the old test got wrong. FastAPI silently drops an unknown
        query key, so a misspelled filter name in a test — or in the SPA — fails
        open to an unfiltered search rather than erroring."""
        from app.main import app

        params = {
            p["name"] for p in app.openapi()["paths"]["/api/search"]["get"].get("parameters", [])
        }
        assert "search_mode" in params
        assert "mode" not in params

    def test_an_unknown_search_mode_is_400(self, client, user_token_headers):
        """The value check the old ``mode=keyword`` test could never reach: with
        the wrong key the handler saw the default and answered 200."""
        response = client.get(
            SEARCH_PATH, params={"q": "test", "search_mode": "fuzzy"}, headers=user_token_headers
        )
        assert response.status_code == 400, response.text

    def test_an_unknown_sort_by_is_400(self, client, user_token_headers):
        response = client.get(
            SEARCH_PATH, params={"q": "test", "sort_by": "popularity"}, headers=user_token_headers
        )
        assert response.status_code == 400, response.text

    def test_an_unknown_sort_order_is_400(self, client, user_token_headers):
        response = client.get(
            SEARCH_PATH, params={"q": "test", "sort_order": "sideways"}, headers=user_token_headers
        )
        assert response.status_code == 400, response.text

    def test_a_missing_query_is_422(self, client, user_token_headers):
        response = client.get(SEARCH_PATH, headers=user_token_headers)
        assert response.status_code == 422, response.text

    def test_an_empty_query_is_422(self, client, user_token_headers):
        response = client.get(SEARCH_PATH, params={"q": ""}, headers=user_token_headers)
        assert response.status_code == 422, response.text

    def test_page_zero_is_422(self, client, user_token_headers):
        response = client.get(
            SEARCH_PATH, params={"q": "test", "page": 0}, headers=user_token_headers
        )
        assert response.status_code == 422, response.text

    def test_a_page_size_above_the_cap_is_422(self, client, user_token_headers):
        """An unbounded page size is a cheap way to pull the whole index in one
        request; the cap is the control."""
        from app.core.constants import SEARCH_MAX_PAGE_SIZE

        response = client.get(
            SEARCH_PATH,
            params={"q": "test", "page_size": SEARCH_MAX_PAGE_SIZE + 1},
            headers=user_token_headers,
        )
        assert response.status_code == 422, response.text

    def test_unauthenticated_is_401(self, client):
        response = client.get(SEARCH_PATH, params={"q": "test"})
        assert response.status_code == 401, response.text


# --------------------------------------------------------------------------- #
# Every filter actually reaches the service                                    #
# --------------------------------------------------------------------------- #
class TestFilterForwarding:
    def test_every_filter_is_forwarded_to_the_search_service(
        self, client, user_token_headers, search_recorder
    ):
        """11 of the 19 query parameters were never varied by any test. A filter
        accepted by the signature and then dropped from the service call is
        exactly the failure this catches — the response shape is identical either
        way."""
        response = client.get(
            SEARCH_PATH,
            params={
                "q": "budget",
                "page": 2,
                "page_size": 5,
                "speakers": ["Alice", "Bob"],
                "tags": ["finance"],
                "date_from": "2026-01-01",
                "date_to": "2026-02-01",
                "sort_by": "duration",
                "sort_order": "asc",
                "search_mode": "keyword",
                "file_type": ["audio"],
                "collection_id": 7,
                "min_duration": 10.5,
                "max_duration": 900.0,
                "min_file_size": 1000,
                "max_file_size": 2_000_000,
                "language": "en",
                "title_filter": "quarterly",
            },
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text
        assert len(search_recorder) == 1
        sent = search_recorder[0]
        assert sent["query"] == "budget"
        assert sent["page"] == 2
        assert sent["page_size"] == 5
        assert sent["speakers"] == ["Alice", "Bob"]
        assert sent["tags"] == ["finance"]
        assert sent["date_from"] == "2026-01-01"
        assert sent["date_to"] == "2026-02-01"
        assert sent["sort_by"] == "duration"
        assert sent["sort_order"] == "asc"
        assert sent["search_mode"] == "keyword"
        assert sent["file_type"] == ["audio"]
        assert sent["collection_id"] == 7
        assert sent["min_duration"] == 10.5
        assert sent["max_duration"] == 900.0
        assert sent["min_file_size"] == 1000
        assert sent["max_file_size"] == 2_000_000
        assert sent["language"] == "en"
        assert sent["title_filter"] == "quarterly"

    def test_the_search_is_scoped_to_the_caller(
        self, client, user_token_headers, normal_user, search_recorder
    ):
        """``user_id`` comes from the resolved context, never the request — the
        service applies it as the accessibility filter."""
        client.get(SEARCH_PATH, params={"q": "test"}, headers=user_token_headers)
        assert search_recorder[0]["user_id"] == normal_user.id

    def test_personal_scope_sends_no_organization_id(
        self, client, user_token_headers, search_recorder
    ):
        """Community invariance: no org context means no org filter, not org 0."""
        client.get(SEARCH_PATH, params={"q": "test"}, headers=user_token_headers)
        assert search_recorder[0]["organization_id"] is None

    def test_the_file_uuid_scope_is_forwarded(self, client, user_token_headers, search_recorder):
        """The in-page find bar's single-file scope. Dropping it silently widens a
        find-in-transcript to the whole library."""
        scoped = str(uuid_pkg.uuid4())
        client.get(
            SEARCH_PATH, params={"q": "test", "file_uuid": scoped}, headers=user_token_headers
        )
        assert search_recorder[0]["file_uuid"] == scoped


# --------------------------------------------------------------------------- #
# The takedown gate on search RESULTS                                          #
# --------------------------------------------------------------------------- #
class TestQuarantinedHitsAreDropped:
    """``_drop_quarantined_search_hits`` had zero HTTP coverage. The transcript
    index has no quarantine field, so this DB pass is the ONLY thing keeping a
    taken-down file's snippets out of search results — the file 404s on
    detail/stream, but its transcript text would still be quoted back."""

    def test_a_quarantined_file_is_dropped_from_the_page(
        self, client, user_token_headers, normal_user, db_session, stub_search
    ):
        visible = _make_file(db_session, normal_user, quarantined=False)
        hidden = _make_file(db_session, normal_user, quarantined=True)
        stub_search([_hit(str(visible.uuid), visible.id), _hit(str(hidden.uuid), hidden.id)])

        response = client.get(SEARCH_PATH, params={"q": "test"}, headers=user_token_headers)
        assert response.status_code == 200, response.text
        returned = {row["file_uuid"] for row in response.json()["results"]}
        assert returned == {str(visible.uuid)}

    def test_the_reported_totals_are_trimmed_with_the_page(
        self, client, user_token_headers, normal_user, db_session, stub_search
    ):
        """Leaving the counts alone would render "2 results" above one row and
        page the user into an empty second page."""
        visible = _make_file(db_session, normal_user, quarantined=False)
        hidden = _make_file(db_session, normal_user, quarantined=True)
        stub_search([_hit(str(visible.uuid), visible.id), _hit(str(hidden.uuid), hidden.id)])

        body = client.get(SEARCH_PATH, params={"q": "test"}, headers=user_token_headers).json()
        assert body["total_results"] == 1
        assert body["total_files"] == 1

    def test_an_admin_still_sees_the_quarantined_file(
        self, client, admin_token_headers, admin_user, db_session, stub_search
    ):
        """The control for the two tests above: same code path, opposite outcome,
        decided only by the caller's role. Without it, a gate that dropped
        *everything* would look like a pass."""
        visible = _make_file(db_session, admin_user, quarantined=False)
        hidden = _make_file(db_session, admin_user, quarantined=True)
        stub_search([_hit(str(visible.uuid), visible.id), _hit(str(hidden.uuid), hidden.id)])

        response = client.get(SEARCH_PATH, params={"q": "test"}, headers=admin_token_headers)
        assert response.status_code == 200, response.text
        returned = {row["file_uuid"] for row in response.json()["results"]}
        assert returned == {str(visible.uuid), str(hidden.uuid)}

    def test_a_clean_page_is_returned_untouched(
        self, client, user_token_headers, normal_user, db_session, stub_search
    ):
        visible = _make_file(db_session, normal_user, quarantined=False)
        stub_search([_hit(str(visible.uuid), visible.id)])

        body = client.get(SEARCH_PATH, params={"q": "test"}, headers=user_token_headers).json()
        assert [row["file_uuid"] for row in body["results"]] == [str(visible.uuid)]
        assert body["total_results"] == 1


# --------------------------------------------------------------------------- #
# Live round trips (need the index)                                            #
# --------------------------------------------------------------------------- #
@requires_opensearch
class TestLiveSearch:
    def test_search_returns_the_documented_envelope(self, client, user_token_headers):
        """``results`` is the only key the endpoint has ever emitted; the old
        ``"results" in data or "items" in data`` accepted a shape that does not
        exist."""
        response = client.get(SEARCH_PATH, params={"q": "test"}, headers=user_token_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert "results" in body
        assert body["query"] == "test"

    def test_keyword_mode_is_reported_back(self, client, user_token_headers):
        """Round-trips the parameter the old filter test believed it was setting."""
        response = client.get(
            SEARCH_PATH,
            params={"q": "test", "search_mode": "keyword"},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["search_mode"] == "keyword"
