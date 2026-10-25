"""Search and chat retrieval return a colleague's file through an organization collection.

A chat turn scoped to an org collection resolved the colleague's files in Postgres and then
retrieved **zero** excerpts, because the index filter knew only ``accessible_user_ids`` (owner and
explicit shares). The read-access clause now adds a second arm on the chunk's ``collection_ids``
(``search/access_filter``, governance plan D-3). This runs the REAL clause, built by the REAL
``HybridSearchService._build_filters``, against a REAL index, so what is proven is that OpenSearch
returns the document, not that a dict has the right shape.

Point it at an isolated stack, never the shared dev one::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 \\
        pytest backend/tests/integration/test_org_collection_retrieval_opensearch.py -m integration
"""

from __future__ import annotations

import contextlib
import os
import uuid as uuid_pkg

import pytest

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH): the thing under test is a filter "
            "evaluated by a real cluster."
        ),
    ),
]


@pytest.fixture
def chunk_index(monkeypatch, gov):
    """A throwaway chunks index with the REAL mapping and ``session_scope`` bridged."""
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import indexing_service as svc

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    @contextlib.contextmanager
    def _test_session():
        yield gov.db

    monkeypatch.setattr("app.db.session_utils.session_scope", _test_session)

    name = f"test_org_collection_retrieval_{uuid_pkg.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    try:
        monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
        monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
        svc.reset_neural_pipeline_state()
        yield client, name
    finally:
        client.indices.delete(index=name, ignore=[404])
        svc.reset_neural_pipeline_state()


def _index(media_file, text: str, *, collection_ids: list[int]) -> None:
    from app.services.search.indexing_service import TranscriptIndexingService

    result = TranscriptIndexingService().index_transcript_chunks(
        file_id=media_file.id,
        file_uuid=str(media_file.uuid),
        user_id=media_file.user_id,
        segments=[{"start": 0.0, "end": 5.0, "text": text, "speaker": "Speaker One"}],
        title="Governance retrieval check",
        speakers=["Speaker One"],
        tags=[],
        accessible_user_ids=[media_file.user_id],
        collection_ids=collection_ids,
        organization_id=media_file.organization_id,
    )
    assert result.get("chunk_count", 0) >= 1, f"setup did not index a chunk: {result!r}"


def _visible_file_uuids(client, index: str, user_id: int, org_id: int | None) -> set[str]:
    from app.services.search.hybrid_search_service import HybridSearchService

    filters = HybridSearchService()._build_filters(
        user_id=user_id,
        speakers=None,
        tags=None,
        date_from=None,
        date_to=None,
        organization_id=org_id,
    )
    client.indices.refresh(index=index)
    response = client.search(index=index, body={"size": 50, "query": {"bool": {"filter": filters}}})
    return {hit["_source"]["file_uuid"] for hit in response["hits"]["hits"]}


def test_a_member_retrieves_a_colleagues_chunks_through_the_org_collection(chunk_index, gov):
    client, index = chunk_index
    collection = gov.collection(gov.admin)
    in_collection = gov.file(gov.alice)
    loose = gov.file(gov.alice)
    gov.put(collection, in_collection)
    _index(
        in_collection, "the budget review for the northern region", collection_ids=[collection.id]
    )
    _index(loose, "the budget review for the southern region", collection_ids=[])

    seen = _visible_file_uuids(client, index, gov.bob.id, gov.org.id)

    assert seen == {str(in_collection.uuid)}, "bob reads exactly the file in the collection"
    # Controls: the owner still reads both, and a person outside the org reads neither.
    assert _visible_file_uuids(client, index, gov.alice.id, gov.org.id) == {
        str(in_collection.uuid),
        str(loose.uuid),
    }
    assert _visible_file_uuids(client, index, gov.outsider.id, gov.org.id) == set()
    # Personal scope never sees organization chunks, collection route or not.
    assert _visible_file_uuids(client, index, gov.bob.id, None) == set()


def test_search_does_not_return_files_reachable_only_via_restricted_collection(chunk_index, gov):
    """Restricting a collection removes it as a route at once: no reindex, no TTL."""
    client, index = chunk_index
    collection = gov.collection(gov.alice)
    media_file = gov.file(gov.alice)
    gov.put(collection, media_file)
    _index(media_file, "the layoff plan for the northern region", collection_ids=[collection.id])

    assert _visible_file_uuids(client, index, gov.bob.id, gov.org.id) == {str(media_file.uuid)}

    collection.visibility = "restricted"
    gov.db.commit()

    assert _visible_file_uuids(client, index, gov.bob.id, gov.org.id) == set()
    # Controls: an organization admin reads it through the collection, the file's owner by
    # the per-file term, and nobody lost anything but the unnamed member.
    assert _visible_file_uuids(client, index, gov.admin.id, gov.org.id) == {str(media_file.uuid)}
    assert _visible_file_uuids(client, index, gov.alice.id, gov.org.id) == {str(media_file.uuid)}
