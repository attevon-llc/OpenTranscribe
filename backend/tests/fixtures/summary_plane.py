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
import uuid as uuid_pkg

import pytest


@pytest.fixture
def summary_plane(monkeypatch, db_session):
    """A throwaway v6 index standing in for ``transcript_chunks``, for one test.

    Never touches the shared index: a randomly named index is created and deleted around the
    test. Neural search is OFF so the leg is pure BM25 -- deterministic, and independent of
    the cluster's ML Commons state. The service's own session is pointed at the test session
    so rows the test committed are the rows the indexer reads.
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

    name = f"test_summary_api_{uuid_pkg.uuid4().hex[:12]}"
    client.indices.create(index=name, body=svc._get_index_body_with_dimension(384))
    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", name)
    monkeypatch.setattr(settings, "OPENSEARCH_NEURAL_SEARCH_ENABLED", False)
    svc.reset_neural_pipeline_state()
    hybrid.reset_neural_search_state()
    try:
        yield client
    finally:
        client.indices.delete(index=name, ignore=[404])
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
