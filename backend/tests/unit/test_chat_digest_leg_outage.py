"""A failed digest leg must be flagged, not read as "no digest matched"."""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch


def _digest_patches(client):
    from app.services.search import chunk_retrieval

    service = MagicMock()
    service._build_filters.return_value = []
    service._generate_query_embedding.return_value = (None, False, False)
    return (
        patch.object(chunk_retrieval, "get_opensearch_client", return_value=client),
        patch.object(chunk_retrieval, "HybridSearchService", return_value=service),
        patch.object(chunk_retrieval, "_build_body", return_value={"_source": []}),
    )


def test_digest_retrieval_failure_is_flagged_not_just_empty():
    from app.services.search.chunk_retrieval import retrieve_digests

    client = MagicMock()
    client.search.side_effect = ConnectionError("down")
    diagnostics: dict = {}
    p1, p2, p3 = _digest_patches(client)
    with p1, p2, p3:
        hits = retrieve_digests("what was decided", user_id=1, diagnostics=diagnostics)

    assert hits == []
    assert diagnostics == {"digest_retrieval_failed": True}


def test_digest_retrieval_with_no_matches_leaves_the_flag_unset():
    from app.services.search.chunk_retrieval import retrieve_digests

    client = MagicMock()
    client.search.return_value = {"hits": {"hits": []}}
    diagnostics: dict = {}
    p1, p2, p3 = _digest_patches(client)
    with p1, p2, p3:
        hits = retrieve_digests("what was decided", user_id=1, diagnostics=diagnostics)

    assert hits == []
    assert diagnostics == {}
