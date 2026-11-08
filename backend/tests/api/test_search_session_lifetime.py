"""The search handlers must not hold the request transaction across OpenSearch.

Concurrency audit F4 (``docs/design/concurrency_foundations_plan.md``): ``GET /api/search``,
``/search/count``, ``/search/suggestions`` and ``/search/filters`` read a few rows through the
request session (hidden files, the collection filter, the redaction policy, a permission
check) and then made their OpenSearch round trips with that transaction still open. The
session comes from ``Depends(get_db)`` and lives until the response is sent, so every
in-flight search pinned a pool connection idle in a transaction for the whole query, and
``/count`` and ``/suggestions`` are polled per keystroke.

These go through the real app, with ``get_db`` yielding a
:class:`~tests.unit._session_lifetime_trackers.TrackedRequestSession` around the test's
savepointed session, so the auth dependencies open the request transaction exactly as in
production. Each OpenSearch stub records whether a transaction was open at the moment it
ran. Each test also asserts the handler really read Postgres (``opened``), so a handler
that skipped the read could not pass by having nothing to hold.
"""

from __future__ import annotations

import uuid

import pytest

from app.db.base import get_db
from app.main import app
from app.models.media import MediaFile
from app.services.search.hybrid_search_service import HybridSearchService
from app.services.search.search_types import SearchHit
from app.services.search.search_types import SearchResponse
from tests.unit._session_lifetime_trackers import TrackedRequestSession


@pytest.fixture
def trackers(client, db_session):
    """Route every request's ``get_db`` through a fresh tracker; return them in order."""
    made: list[TrackedRequestSession] = []

    def _override():
        tracker = TrackedRequestSession(db_session)
        made.append(tracker)
        yield tracker

    app.dependency_overrides[get_db] = _override
    return made


@pytest.fixture
def owned_file(db_session, normal_user):
    media_file = MediaFile(
        uuid=str(uuid.uuid4()),
        filename="search-lifetime.wav",
        title="search lifetime",
        storage_path="media/test/search-lifetime.wav",
        content_type="audio/wav",
        file_size=1,
        status="completed",
        is_public=False,
        user_id=normal_user.id,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _observing(monkeypatch, made: list[TrackedRequestSession], name: str, result):
    def _stub(_self, *_args, **_kwargs):
        made[-1].observe(name)
        return result

    monkeypatch.setattr(HybridSearchService, name, _stub)


def _held(made: list[TrackedRequestSession]) -> list[tuple[str, bool]]:
    return [obs for obs in made[-1].observations if obs[1]]


def test_search_runs_every_opensearch_leg_outside_the_transaction(
    client, user_token_headers, trackers, owned_file, monkeypatch
):
    hit = SearchHit(
        file_uuid=str(owned_file.uuid),
        file_id=owned_file.id,
        title="search lifetime",
        speakers=[],
        tags=[],
        upload_time="2026-03-10T00:00:00+00:00",
        language="en",
    )
    _observing(
        monkeypatch,
        trackers,
        "search",
        SearchResponse(
            query="q",
            results=[hit],
            total_results=1,
            total_files=1,
            page=1,
            page_size=20,
            total_pages=1,
            search_time_ms=1.0,
        ),
    )
    _observing(monkeypatch, trackers, "search_summary_plane", ([], 0))
    _observing(monkeypatch, trackers, "count_matches", 3)

    response = client.get(
        "/api/search",
        params={"q": "lifetime", "sources": ["content", "title", "speaker", "summary"]},
        headers=user_token_headers,
    )

    assert response.status_code == 200, response.text
    tracker = trackers[-1]
    labels = [label for label, _ in tracker.observations]
    # The transcript leg, the summary leg, three per-source counts and the summary count.
    assert labels.count("search") == 1, labels
    assert labels.count("count_matches") == 3, labels
    assert labels.count("search_summary_plane") == 2, labels
    assert _held(trackers) == [], f"held across OpenSearch: {tracker.observations}"
    # Read phase, then the post-filter's own short transaction after the queries.
    assert tracker.opened >= 2, tracker.opened
    body = response.json()
    assert [r["file_uuid"] for r in body["results"]] == [str(owned_file.uuid)]
    assert body["source_counts"]["content"] == 3


def test_count_runs_outside_the_transaction(
    client, user_token_headers, trackers, owned_file, monkeypatch
):
    _observing(monkeypatch, trackers, "count_matches", 5)

    response = client.get(
        "/api/search/count",
        params={"q": "lifetime", "file_uuid": str(owned_file.uuid)},
        headers=user_token_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"total": 5}
    assert trackers[-1].seen == {"count_matches": False}, trackers[-1].observations
    assert trackers[-1].opened >= 1


def test_suggestions_run_outside_the_transaction(client, user_token_headers, trackers, monkeypatch):
    _observing(monkeypatch, trackers, "get_suggestions", [{"type": "title", "text": "life"}])

    response = client.get("/api/search/suggestions", params={"q": "li"}, headers=user_token_headers)

    assert response.status_code == 200, response.text
    assert response.json() == [{"type": "title", "text": "life"}]
    assert trackers[-1].seen == {"get_suggestions": False}, trackers[-1].observations
    assert trackers[-1].opened >= 1


def test_filters_run_outside_the_transaction(client, user_token_headers, trackers, monkeypatch):
    facets: dict[str, object] = {"speakers": [], "tags": [], "date_range": {}}
    _observing(monkeypatch, trackers, "get_available_filters", facets)

    response = client.get("/api/search/filters", headers=user_token_headers)

    assert response.status_code == 200, response.text
    assert response.json() == facets
    assert trackers[-1].seen == {"get_available_filters": False}, trackers[-1].observations
    assert trackers[-1].opened >= 1
