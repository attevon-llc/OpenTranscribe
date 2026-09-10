"""``GET /api/search/suggestions`` speaker-facet leak (issue #817).

``HybridSearchService.get_suggestions`` runs an msearch with two legs — a title
prefix match and a speaker prefix aggregation — and neither carried a quarantine
exclusion. The endpoint used to post-filter the RESPONSE against Postgres, but only
on the title leg (each title suggestion carries a ``file_uuid``); the speaker leg's
aggregation buckets carry no file linkage at all, so there was nothing for that
post-filter to check. A quarantined file's speaker names therefore leaked into
autocomplete for everyone who had access before the takedown, including the file's
own owner, forever.

The fix moves the exclusion INTO the query, on both legs, matching
``get_available_filters``' posture (issue #876): quarantine is Postgres-only, so
the ``must_not`` clause has to be built from a resolved uuid set rather than a field
on the document, and resolving that set fails closed rather than degrading to
"exclude nothing".

OpenSearch is stubbed, matching ``test_search_filters_fail_closed.py`` — but the
stub here additionally HONOURS the ``must_not`` clause it is sent (filtering its own
canned docs by it), so "the speaker/title is absent from the response" is a real
assertion about what a cluster obeying the clause would do, not merely a restatement
of "the clause was present in the recorded body".
"""

from __future__ import annotations

import contextlib
import uuid as uuid_pkg
from typing import Any

import pytest

from app.models.media import MediaFile
from app.models.user import User
from app.services.search import hybrid_search_service as hss

SUGGESTIONS = "/api/search/suggestions"


def _must_not_uuids(query_body: dict[str, Any]) -> set[str]:
    clauses = query_body["query"]["bool"].get("must_not", [])
    uuids: set[str] = set()
    for clause in clauses:
        uuids.update(clause.get("terms", {}).get("file_uuid", []))
    return uuids


class _FakeIndices:
    def exists(self, index: str) -> bool:  # noqa: ARG002 - stub signature parity
        return True


class _FakeSuggestionsCluster:
    """Stub cluster that honours a ``must_not`` clause, like a real one would."""

    def __init__(self) -> None:
        self.indices = _FakeIndices()
        self.msearch_calls: list[list[dict[str, Any]]] = []
        self.title_docs: list[dict[str, str]] = []  # {"title", "file_uuid"}
        self.speaker_docs: list[dict[str, Any]] = []  # {"speaker", "file_uuid", "doc_count"}

    def msearch(self, body: list[dict[str, Any]]) -> dict[str, Any]:
        self.msearch_calls.append(body)
        title_query, speaker_query = body[1], body[3]
        title_excluded = _must_not_uuids(title_query)
        speaker_excluded = _must_not_uuids(speaker_query)

        title_hits = [
            {"_source": {"title": d["title"], "file_uuid": d["file_uuid"]}}
            for d in self.title_docs
            if d["file_uuid"] not in title_excluded
        ]
        speaker_buckets = [
            {"key": d["speaker"], "doc_count": d["doc_count"]}
            for d in self.speaker_docs
            if d["file_uuid"] not in speaker_excluded
        ]
        return {
            "responses": [
                {"hits": {"hits": title_hits}},
                {"aggregations": {"speakers": {"buckets": speaker_buckets}}},
            ]
        }


@pytest.fixture
def fake_cluster(monkeypatch) -> _FakeSuggestionsCluster:
    """Point the service at a stub cluster that would happily serve suggestions."""
    fake = _FakeSuggestionsCluster()
    monkeypatch.setattr(hss, "opensearch_client", fake)
    monkeypatch.setattr(hss, "_index_verified", True)
    return fake


@pytest.fixture
def bridged_session(monkeypatch, db_session):
    """Make ``_quarantined_file_uuids``'s own session the test's savepoint session."""

    @contextlib.contextmanager
    def _test_session():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _test_session)
    return db_session


@pytest.fixture
def broken_session(monkeypatch):
    """Make ``_quarantined_file_uuids``'s own session raise, as a DB outage would."""

    @contextlib.contextmanager
    def _boom():
        raise RuntimeError("simulated transient database outage")
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr("app.db.session_utils.session_scope", _boom)


def _make_file(db_session, owner: User, **overrides) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    defaults = dict(
        uuid=file_uuid,
        user_id=owner.id,
        filename="suggestions-quarantine.wav",
        storage_path=f"x/{file_uuid.hex}.wav",
        file_size=1,
        content_type="audio/wav",
        status="completed",
    )
    defaults.update(overrides)
    media_file = MediaFile(**defaults)
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


# ---------------------------------------------------------------------------
# 1. The finding: the speaker leg carries no file linkage to post-filter
# ---------------------------------------------------------------------------


def test_speaker_facet_excludes_a_quarantined_files_speaker(
    fake_cluster, bridged_session, normal_user
):
    hidden = _make_file(bridged_session, normal_user, is_quarantined=True)
    fake_cluster.speaker_docs = [
        {"speaker": "Zzyzx-Hidden-Speaker", "file_uuid": str(hidden.uuid), "doc_count": 2}
    ]

    service = hss.HybridSearchService()
    suggestions = service.get_suggestions(prefix="zzy", user_id=normal_user.id, is_admin=False)

    assert suggestions == [], (
        "a quarantined file's speaker name reached autocomplete — the speaker "
        "leg carries no file linkage, so nothing downstream could have caught it"
    )
    speaker_query = fake_cluster.msearch_calls[0][3]
    assert str(hidden.uuid) in _must_not_uuids(speaker_query)


# ---------------------------------------------------------------------------
# 2. Non-regression: the title leg must keep excluding, now inside the query
# ---------------------------------------------------------------------------


def test_title_leg_still_excludes_a_quarantined_files_title(
    fake_cluster, bridged_session, normal_user
):
    hidden = _make_file(bridged_session, normal_user, is_quarantined=True)
    visible = _make_file(bridged_session, normal_user)
    fake_cluster.title_docs = [
        {"title": "hidden recording", "file_uuid": str(hidden.uuid)},
        {"title": "visible recording", "file_uuid": str(visible.uuid)},
    ]

    service = hss.HybridSearchService()
    suggestions = service.get_suggestions(prefix="rec", user_id=normal_user.id, is_admin=False)

    assert [s["text"] for s in suggestions] == ["visible recording"]
    title_query = fake_cluster.msearch_calls[0][1]
    assert str(hidden.uuid) in _must_not_uuids(title_query)


# ---------------------------------------------------------------------------
# 3. Control: nothing quarantined -> no clause added at all
# ---------------------------------------------------------------------------


def test_no_must_not_clause_when_nothing_is_quarantined(fake_cluster, bridged_session, normal_user):
    visible = _make_file(bridged_session, normal_user)
    fake_cluster.title_docs = [{"title": "visible recording", "file_uuid": str(visible.uuid)}]

    service = hss.HybridSearchService()
    suggestions = service.get_suggestions(prefix="rec", user_id=normal_user.id, is_admin=False)

    assert [s["text"] for s in suggestions] == ["visible recording"]
    title_query = fake_cluster.msearch_calls[0][1]
    speaker_query = fake_cluster.msearch_calls[0][3]
    assert "must_not" not in title_query["query"]["bool"]
    assert "must_not" not in speaker_query["query"]["bool"]


# ---------------------------------------------------------------------------
# 4. Admin bypass control, exercised through the real endpoint
# ---------------------------------------------------------------------------


def test_an_admin_bypasses_the_exclusion(
    client, admin_token_headers, admin_user, db_session, fake_cluster
):
    hidden = _make_file(db_session, admin_user, is_quarantined=True)
    fake_cluster.title_docs = [{"title": "hidden recording", "file_uuid": str(hidden.uuid)}]

    response = client.get(SUGGESTIONS, headers=admin_token_headers, params={"q": "rec"})

    assert response.status_code == 200, response.text
    assert [s["text"] for s in response.json()] == ["hidden recording"]
    title_query = fake_cluster.msearch_calls[0][1]
    assert "must_not" not in title_query["query"]["bool"]


# ---------------------------------------------------------------------------
# 5. Fail closed on a DB outage
# ---------------------------------------------------------------------------


def test_the_endpoint_fails_closed_on_a_db_outage(
    client, user_token_headers, fake_cluster, broken_session
):
    """RED FIRST against HEAD: before this fix there was no exclusion mechanism
    on this leg to fail at all — the endpoint answered 200 with unfiltered
    suggestions regardless of what Postgres could or could not tell it."""
    response = client.get(SUGGESTIONS, headers=user_token_headers, params={"q": "rec"})

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "Search suggestions are temporarily unavailable."
    assert fake_cluster.msearch_calls == []
