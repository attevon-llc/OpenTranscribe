"""Seed the OpenSearch summary plane from Postgres for API-level summary-search tests.

Since #963 the summary leg of ``GET /api/search`` queries ``doc_type: "summary"`` documents in
the ``transcript_chunks`` index, not Postgres. A test that only inserts a ``MediaFile`` row with
``summary_data`` therefore finds nothing. These helpers write the plane the way production does
-- by running the ``index_file_summary`` task body, which re-reads Postgres and computes the
ACL/tag/collection metadata itself -- so a test exercises the real write path and the real read
path together instead of mocking either.

Every mutation a test makes to something the plane denormalises (tags, collections, shares,
the summary itself) must be followed by :func:`sync_summary_plane`, exactly as the product
re-indexes after those same changes.
"""

from __future__ import annotations

import contextlib
import os
import uuid as uuid_pkg

import pytest


@pytest.fixture(scope="session")
def _summary_plane_index():
    """One throwaway v6 index per pytest process (so per xdist worker), never the shared one.

    It is created once, not per test: with a full parallel run, N workers each creating and
    deleting an index per test serialise on the cluster state and time out. Isolation between
    tests comes from emptying it in :func:`summary_plane`, not from a fresh index.
    """
    # CI forces SKIP_OPENSEARCH=True (there is no cluster there), and a client object still
    # builds against localhost:9200, so asking it for an index raises ConnectionError at
    # SETUP and 43 tests ERROR instead of being skipped. These tests need the real plane;
    # they run in the local gate (run-integration-tests.sh), where the stack is up.
    if os.environ.get("SKIP_OPENSEARCH", "").lower() == "true":
        pytest.skip(
            "summary-plane API tests need a reachable OpenSearch (SKIP_OPENSEARCH is set); "
            "they run in the local gate against the dev stack"
        )

    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import indexing_service as svc

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    name = f"test_summary_api_{uuid_pkg.uuid4().hex[:12]}"
    # A single-node cluster creates indices one at a time (~1 s each), and every xdist worker
    # asks for one at the same moment, so the last in the queue waits for all the others. The
    # client's default 10 s read timeout is shorter than that wait; the call is not slow, it is
    # queued.
    client.indices.create(
        index=name,
        body=svc._get_index_body_with_dimension(384),
        params={"request_timeout": 180},
    )
    try:
        yield name
    finally:
        client.indices.delete(index=name, ignore=[404])


@pytest.fixture
def summary_plane(monkeypatch, db_session, _summary_plane_index):
    """Point the summary leg at the worker's throwaway index, empty, for one test.

    Neural search is OFF so the leg is pure BM25 -- deterministic, and independent of the
    cluster's ML Commons state. The service's own session is pointed at the test session so
    rows the test committed are the rows the indexer reads. The index is emptied afterwards:
    several tests act as an admin, who sees every owner's documents, so a document left by one
    test would be counted by the next.
    """
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.services.search import hybrid_search_service as hybrid
    from app.services.search import indexing_service as svc

    client = get_opensearch_client()
    assert client is not None, "SKIP_OPENSEARCH said a cluster was reachable but it is not"

    @contextlib.contextmanager
    def _test_session():
        yield db_session

    monkeypatch.setattr("app.db.session_utils.session_scope", _test_session)
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", _summary_plane_index)
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
    svc.reset_neural_pipeline_state()
    hybrid.reset_neural_search_state()
    try:
        yield client
    finally:
        client.delete_by_query(
            index=_summary_plane_index,
            body={"query": {"match_all": {}}},
            refresh=True,
            conflicts="proceed",
        )
        svc.reset_neural_pipeline_state()
        hybrid.reset_neural_search_state()


def sync_summary_plane(media_file) -> None:
    """Rebuild ``media_file``'s summary plane from Postgres and make it searchable."""
    from app.core.config import settings
    from app.services.opensearch_service import get_opensearch_client
    from app.tasks.search_indexing_task import index_file_summary

    result = index_file_summary(media_file.id)
    assert result["status"] == "success", f"summary plane write failed: {result!r}"

    client = get_opensearch_client()
    assert client is not None
    client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
