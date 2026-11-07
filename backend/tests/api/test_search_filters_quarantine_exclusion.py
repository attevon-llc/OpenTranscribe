"""``GET /search/filters`` keeps a taken-down file out of every facet (issues #876, #1253).

The facets are aggregations: there is no hit list a post-filter could correct, so the takedown
has to be excluded inside the query. It used to be a Postgres-resolved ``must_not terms`` list
of quarantined file uuids, which had to fail closed (503) on a database error and past its
10,000-id cap. Now the takedown stamps ``availability: quarantined`` on the file's documents and
the shared ACL clause excludes it, so the facets answer at any number of taken-down files with no
list to resolve (the body-level property is pinned in
``tests/unit/test_quarantine_index_availability.py``).

OpenSearch is stubbed, but the stub builds its buckets only from the documents the body's
``filter`` list admits, so "the speaker is absent from the facets" is a statement about what a
cluster obeying the body would return. The live twin is
``tests/integration/test_search_filters_quarantine.py``.
"""

from __future__ import annotations

import uuid as uuid_pkg
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.media import MediaFile
from app.models.user import User
from app.services.file_availability import index_availability
from app.services.search import search_infra
from app.services.search import suggestions
from tests.fixtures.opensearch_filter_eval import matches_all

FILTERS = "/api/search/filters"
HIDDEN_SPEAKER = "Zzyzx-Facet-Speaker"
VISIBLE_SPEAKER = "Plain-Facet-Speaker"


class _FacetCluster:
    """Builds speaker/tag buckets from the documents the body's ``filter`` list admits."""

    def __init__(self) -> None:
        self.indices = SimpleNamespace(exists=lambda index: True)
        self.docs: list[dict[str, Any]] = []
        self.bodies: list[dict[str, Any]] = []

    def search(self, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        self.bodies.append(body)
        admitted = [d for d in self.docs if matches_all(d, body["query"]["bool"]["filter"])]
        return {
            "aggregations": {
                "speakers": {"buckets": [{"key": d["speaker"], "doc_count": 1} for d in admitted]},
                "tags": {
                    "buckets": [{"key": t, "doc_count": 1} for d in admitted for t in d["tags"]]
                },
                "date_range": {},
            }
        }


@pytest.fixture
def fake_cluster(monkeypatch) -> _FacetCluster:
    fake = _FacetCluster()
    monkeypatch.setattr(suggestions._client, "opensearch_client", fake)
    monkeypatch.setattr(search_infra, "_index_verified", True)
    return fake


def _file(db_session, owner: User, **overrides: Any) -> MediaFile:
    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        filename="facet-quarantine.wav",
        storage_path=f"x/{uuid_pkg.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
        **overrides,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _seed(cluster: _FacetCluster, db_session, owner: User) -> MediaFile:
    hidden = _file(db_session, owner, is_quarantined=True)
    visible = _file(db_session, owner)
    for media_file, speaker in ((hidden, HIDDEN_SPEAKER), (visible, VISIBLE_SPEAKER)):
        cluster.docs.append(
            {
                "file_uuid": str(media_file.uuid),
                "accessible_user_ids": [owner.id],
                "doc_type": "chunk",
                "availability": index_availability(media_file),
                "speaker": speaker,
                "tags": [f"tag-{speaker}"],
            }
        )
    return hidden


def test_a_quarantined_files_speaker_and_tags_are_not_facets(
    client, user_token_headers, normal_user, db_session, fake_cluster
):
    hidden = _seed(fake_cluster, db_session, normal_user)

    response = client.get(FILTERS, headers=user_token_headers)

    assert response.status_code == 200, response.text
    assert [s["name"] for s in response.json()["speakers"]] == [VISIBLE_SPEAKER]
    assert [t["name"] for t in response.json()["tags"]] == [f"tag-{VISIBLE_SPEAKER}"]
    assert str(hidden.uuid) not in str(fake_cluster.bodies[0]), "no per-file id list"


def test_an_admin_keeps_a_quarantined_files_facets(
    client, admin_token_headers, admin_user, db_session, fake_cluster
):
    _seed(fake_cluster, db_session, admin_user)

    response = client.get(FILTERS, headers=admin_token_headers)

    assert response.status_code == 200, response.text
    assert sorted(s["name"] for s in response.json()["speakers"]) == sorted(
        [HIDDEN_SPEAKER, VISIBLE_SPEAKER]
    )
