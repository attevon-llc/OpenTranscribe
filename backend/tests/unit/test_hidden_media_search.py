"""Search leaves out the caller's hidden files, read-side, for the caller only (issue #1102).

The index documents are shared by everyone who can read a file, so hide is never stamped on
them. Each query of the caller instead carries a ``must_not terms file_uuid`` clause, except a
request scoped to one file the caller picked. These tests capture what the routes hand to the
search service and what the builders put in the OpenSearch body.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from fastapi import status

from app.api.endpoints.search_helpers import _drop_quarantined_search_hits
from app.models.media import MediaFile
from app.services import hidden_media_service
from app.services.search import suggestions
from app.services.search.filters import _build_filters
from app.services.search.hybrid_search_service import HybridSearchService

_MUST_NOT = "must_not"


def _file(db_session, owner) -> MediaFile:
    file_uuid = str(uuid.uuid4())
    row = MediaFile(
        uuid=file_uuid,
        filename="Larkspire review.wav",
        title="Larkspire review",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
        user_id=owner.id,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _hidden(db_session, user, media_file) -> str:
    hidden_media_service.hide(db_session, user.id, [media_file.id])
    db_session.commit()
    return str(media_file.uuid)


def _excluded(clauses) -> set[str]:
    out: set[str] = set()
    for clause in clauses:
        inner = clause.get("bool", {}).get(_MUST_NOT, {})
        for c in inner if isinstance(inner, list) else [inner]:
            out |= set(c.get("terms", {}).get("file_uuid", []))
    return out


# --- the builder ------------------------------------------------------------------------------


def test_the_filter_builder_excludes_the_given_files():
    clauses = _build_filters(1, None, None, None, None, exclude_file_uuids=["a", "b"])
    assert _excluded(clauses) == {"a", "b"}
    assert _excluded(_build_filters(1, None, None, None, None)) == set(), "control"


def test_the_response_cache_key_names_the_hidden_set(monkeypatch):
    """A page cached for one hide set must not be served after a hide or an unhide."""
    from app.services.search import hybrid_search_service as hss

    keys: list[str] = []
    monkeypatch.setattr(hss, "get_opensearch_client", lambda: object())
    monkeypatch.setattr(hss, "_resolve_redaction_config_for_cache", lambda *_a, **_k: None)
    monkeypatch.setattr(hss, "_redaction_policy_fingerprint", lambda _cfg: "p")
    monkeypatch.setattr(hss, "_search_corpus_version", lambda: "v")

    def _cached(key):
        keys.append(key)
        return SimpleNamespace(results=[])

    monkeypatch.setattr(hss, "_get_cached_response", _cached)
    service = HybridSearchService()
    service.search(query="q", user_id=1)
    service.search(query="q", user_id=1, exclude_file_uuids=["x"])
    service.search(query="q", user_id=1, exclude_file_uuids=["y"])
    assert len(set(keys)) == 3


# --- the routes -------------------------------------------------------------------------------


def _capture_search(monkeypatch) -> dict:
    seen: dict = {}

    def _search(self, **kwargs):
        seen.update(kwargs)
        return self._empty_response(kwargs["query"], kwargs["page"], kwargs["page_size"])

    monkeypatch.setattr(HybridSearchService, "search", _search)
    return seen


def test_search_excludes_the_callers_hidden_files(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    seen = _capture_search(monkeypatch)
    hidden = _hidden(db_session, normal_user, _file(db_session, normal_user))
    shown = str(_file(db_session, normal_user).uuid)

    response = client.get("/api/search", headers=user_token_headers, params={"q": "larkspire"})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert seen["exclude_file_uuids"] == [hidden]
    assert shown not in seen["exclude_file_uuids"]


def test_search_scoped_to_one_picked_file_keeps_it(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    seen = _capture_search(monkeypatch)
    hidden = _hidden(db_session, normal_user, _file(db_session, normal_user))

    response = client.get(
        "/api/search", headers=user_token_headers, params={"q": "larkspire", "file_uuid": hidden}
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert not seen["exclude_file_uuids"]


def test_another_users_search_is_not_given_my_hidden_files(
    client, other_user_auth_headers, normal_user, db_session, monkeypatch
):
    seen = _capture_search(monkeypatch)
    _hidden(db_session, normal_user, _file(db_session, normal_user))

    response = client.get("/api/search", headers=other_user_auth_headers, params={"q": "lark"})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert seen["exclude_file_uuids"] == []


def test_the_whole_corpus_count_excludes_hidden_files_and_a_file_count_does_not(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    calls: list = []

    def _count(self, query, **kwargs):
        calls.append(kwargs.get("exclude_file_uuids"))
        return 0

    monkeypatch.setattr(HybridSearchService, "count_matches", _count)
    hidden = _hidden(db_session, normal_user, _file(db_session, normal_user))

    assert (
        client.get(
            "/api/search/count", headers=user_token_headers, params={"q": "lark"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/search/count",
            headers=user_token_headers,
            params={"q": "lark", "file_uuid": hidden},
        ).status_code
        == 200
    )
    assert calls == [[hidden], None]


def test_suggestions_and_facets_carry_the_exclusion(monkeypatch):
    bodies: list = []

    class _Client:
        def msearch(self, body):
            bodies.extend(b for b in body if "query" in b)
            return {"responses": []}

        def search(self, index, body):
            bodies.append(body)
            return {"aggregations": {}}

    monkeypatch.setattr(suggestions._client, "opensearch_client", _Client())
    monkeypatch.setattr(suggestions, "_chunks_index_verified", lambda *_a: True)

    suggestions.get_suggestions("la", 1, is_admin=True, exclude_file_uuids=["h"])
    suggestions.get_available_filters(1, is_admin=True, exclude_file_uuids=["h"])

    assert len(bodies) == 3
    for body in bodies:
        assert _excluded(body["query"]["bool"]["filter"]) == {"h"}


def test_the_page_post_filter_drops_a_hidden_hit(db_session, normal_user):
    kept = _file(db_session, normal_user)
    hidden = _file(db_session, normal_user)
    response = SimpleNamespace(
        results=[
            SimpleNamespace(file_uuid=str(kept.uuid)),
            SimpleNamespace(file_uuid=str(hidden.uuid)),
        ],
        total_results=2,
        total_files=2,
    )

    trimmed = _drop_quarantined_search_hits(
        db_session, response, hidden_uuids=frozenset({str(hidden.uuid)})
    )

    assert [h.file_uuid for h in trimmed.results] == [str(kept.uuid)]
    assert trimmed.total_files == 1
    assert len(response.results) == 2, "the input (maybe a cache entry) is never mutated"
