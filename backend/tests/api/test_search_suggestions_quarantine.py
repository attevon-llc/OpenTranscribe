"""``GET /api/search/suggestions`` speaker-facet leak (issue #817).

``HybridSearchService.get_suggestions`` runs an msearch with two legs — a title
prefix match and a speaker prefix aggregation — and neither carried a quarantine
exclusion. The endpoint used to post-filter the RESPONSE against Postgres, but only
on the title leg (each title suggestion carries a ``file_uuid``); the speaker leg's
aggregation buckets carry no file linkage at all, so there was nothing for that
post-filter to check. A quarantined file's speaker names therefore leaked into
autocomplete for everyone who had access before the takedown, including the file's
own owner, forever.

The exclusion is IN the query, on both legs: the takedown stamps
``availability: quarantined`` on the file's documents (issue #1253) and the shared ACL
clause excludes it, so there is no id list to resolve and nothing to fail closed on.

OpenSearch is stubbed, but the stub HONOURS the ``filter`` list it is sent (evaluating it
against its canned documents), so "the speaker/title is absent from the response" is a real
assertion about what a cluster obeying the body would do, not merely a restatement of "the
clause was present in the recorded body".
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import pytest

from app.models.media import MediaFile
from app.models.user import User
from app.services.file_availability import index_availability
from app.services.search import hybrid_search_service as hss
from app.services.search import search_infra
from app.services.search import suggestions
from tests.fixtures.opensearch_filter_eval import matches_all

SUGGESTIONS = "/api/search/suggestions"


class _FakeIndices:
    def exists(self, index: str) -> bool:  # stub signature parity
        return True


class _FakeSuggestionsCluster:
    """Stub cluster that honours each leg's ``filter`` list, like a real one would."""

    def __init__(self) -> None:
        self.indices = _FakeIndices()
        self.msearch_calls: list[list[dict[str, Any]]] = []
        self.title_docs: list[dict[str, Any]] = []
        self.speaker_docs: list[dict[str, Any]] = []  # plus "speaker", "doc_count"

    def msearch(self, body: list[dict[str, Any]]) -> dict[str, Any]:
        self.msearch_calls.append(body)
        title_filter = body[1]["query"]["bool"]["filter"]
        speaker_filter = body[3]["query"]["bool"]["filter"]
        title_hits = [
            {"_source": {"title": d["title"], "file_uuid": d["file_uuid"]}}
            for d in self.title_docs
            if matches_all(d, title_filter)
        ]
        speaker_buckets = [
            {"key": d["speaker"], "doc_count": d["doc_count"]}
            for d in self.speaker_docs
            if matches_all(d, speaker_filter)
        ]
        return {
            "responses": [
                {"hits": {"hits": title_hits}},
                {"aggregations": {"speakers": {"buckets": speaker_buckets}}},
            ]
        }


def _doc(owner: User, media_file: MediaFile, **fields: Any) -> dict[str, Any]:
    """A chunk document as the indexer writes it, availability from the row."""
    return {
        "file_uuid": str(media_file.uuid),
        "accessible_user_ids": [owner.id],
        "doc_type": "chunk",
        "availability": index_availability(media_file),
        **fields,
    }


@pytest.fixture
def fake_cluster(monkeypatch) -> _FakeSuggestionsCluster:
    """Point the service at a stub cluster that would happily serve suggestions."""
    fake = _FakeSuggestionsCluster()
    monkeypatch.setattr(suggestions._client, "opensearch_client", fake)
    monkeypatch.setattr(search_infra, "_index_verified", True)
    return fake


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


def test_speaker_facet_excludes_a_quarantined_files_speaker(fake_cluster, db_session, normal_user):
    hidden = _make_file(db_session, normal_user, is_quarantined=True)
    fake_cluster.speaker_docs = [
        _doc(normal_user, hidden, speaker="Zzyzx-Hidden-Speaker", doc_count=2)
    ]

    service = hss.HybridSearchService()
    suggestions = service.get_suggestions(prefix="zzy", user_id=normal_user.id, is_admin=False)

    assert suggestions == [], (
        "a quarantined file's speaker name reached autocomplete — the speaker "
        "leg carries no file linkage, so nothing downstream could have caught it"
    )


# ---------------------------------------------------------------------------
# 2. The title leg excludes it too, inside the query
# ---------------------------------------------------------------------------


def test_title_leg_excludes_a_quarantined_files_title(fake_cluster, db_session, normal_user):
    hidden = _make_file(db_session, normal_user, is_quarantined=True)
    visible = _make_file(db_session, normal_user)
    fake_cluster.title_docs = [
        _doc(normal_user, hidden, title="hidden recording"),
        _doc(normal_user, visible, title="visible recording"),
    ]

    service = hss.HybridSearchService()
    suggestions = service.get_suggestions(prefix="rec", user_id=normal_user.id, is_admin=False)

    assert [s["text"] for s in suggestions] == ["visible recording"]


# ---------------------------------------------------------------------------
# 3. No per-file id list in either leg
# ---------------------------------------------------------------------------


def test_no_file_id_list_is_sent_for_the_quarantine(fake_cluster, db_session, normal_user):
    hidden = _make_file(db_session, normal_user, is_quarantined=True)
    fake_cluster.title_docs = [_doc(normal_user, hidden, title="hidden recording")]

    hss.HybridSearchService().get_suggestions(prefix="rec", user_id=normal_user.id)

    assert str(hidden.uuid) not in str(fake_cluster.msearch_calls[0])


# ---------------------------------------------------------------------------
# 4. Review exemption control, exercised through the real endpoint
# ---------------------------------------------------------------------------


def test_an_admin_keeps_quarantined_suggestions(
    client, admin_token_headers, admin_user, db_session, fake_cluster
):
    hidden = _make_file(db_session, admin_user, is_quarantined=True)
    fake_cluster.title_docs = [_doc(admin_user, hidden, title="hidden recording")]

    response = client.get(SUGGESTIONS, headers=admin_token_headers, params={"q": "rec"})

    assert response.status_code == 200, response.text
    assert [s["text"] for s in response.json()] == ["hidden recording"]


def test_a_plain_user_does_not_get_them_through_the_endpoint(
    client, user_token_headers, normal_user, db_session, fake_cluster
):
    hidden = _make_file(db_session, normal_user, is_quarantined=True)
    fake_cluster.title_docs = [_doc(normal_user, hidden, title="hidden recording")]

    response = client.get(SUGGESTIONS, headers=user_token_headers, params={"q": "rec"})

    assert response.status_code == 200, response.text
    assert response.json() == []
