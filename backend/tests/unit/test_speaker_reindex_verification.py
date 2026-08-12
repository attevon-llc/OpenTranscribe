"""Verification before the destructive half of a speaker-index "rename".

``aliases._reindex_and_alias`` copies an index, verifies the copy, then DELETES
the source. Voiceprint embeddings cannot be recomputed without the original
media, so the verification is the only thing standing between a partial reindex
and permanent loss — and it used to accept losing 5% of the documents.
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.opensearch_service import aliases

_SOURCE = "speakers"
_TARGET = "speakers_v3"
_ALIAS = "speakers"

_CLIENT_PATH = "app.services.opensearch_service.client.opensearch_client"


def _stub_client(source_count: int, target_count: int) -> MagicMock:
    """Build a stub client whose reindex lands ``target_count`` of ``source_count``.

    Args:
        source_count: Documents ``count`` reports for the source index.
        target_count: Documents ``count`` reports for the target index.

    Returns:
        A ``MagicMock`` shaped like the OpenSearch client ``aliases`` uses.
    """
    client = MagicMock()
    client.indices.get.return_value = {
        _SOURCE: {"settings": {"index": {"number_of_shards": 1}}, "mappings": {}}
    }

    def _count(index: str, **_kwargs):
        return {"count": source_count if index == _SOURCE else target_count}

    client.count.side_effect = _count
    return client


def _deleted_indices(client: MagicMock) -> list[str]:
    """Index names passed to ``indices.delete``, in call order."""
    return [call.kwargs.get("index") for call in client.indices.delete.call_args_list]


def test_reindex_one_document_short_refuses_to_delete_the_source():
    """Defect: ``target_count < source_count * 0.95`` licensed losing 1 doc in 20.

    99 of 100 documents used to pass verification, after which the source index —
    the only other copy of those embeddings — was deleted. At 50k voiceprints
    that tolerance is 2,500 speakers who can never be re-identified.
    """
    client = _stub_client(source_count=100, target_count=99)

    with patch(_CLIENT_PATH, client), pytest.raises(RuntimeError, match="verification failed"):
        aliases._reindex_and_alias(_SOURCE, _TARGET, _ALIAS)

    assert _deleted_indices(client) == []


def test_reindex_at_the_boundary_of_completeness_is_rejected():
    """One missing document out of two is still a missing document.

    Pins the boundary itself: 1 of 2 is 50% — comfortably inside no tolerance,
    but it is the smallest case where "nearly all" and "all" differ.
    """
    client = _stub_client(source_count=2, target_count=1)

    with patch(_CLIENT_PATH, client), pytest.raises(RuntimeError, match="verification failed"):
        aliases._reindex_and_alias(_SOURCE, _TARGET, _ALIAS)

    assert _deleted_indices(client) == []


def test_reindex_with_every_document_deletes_the_source_and_aliases():
    """Control: a complete copy still completes the rename.

    Without this the tightened check could be satisfied by a function that never
    deletes anything, and the migration would silently stop working.
    """
    client = _stub_client(source_count=100, target_count=100)

    with patch(_CLIENT_PATH, client):
        aliases._reindex_and_alias(_SOURCE, _TARGET, _ALIAS)

    assert _deleted_indices(client) == [_SOURCE]
    client.indices.put_alias.assert_called_once_with(index=_TARGET, name=_ALIAS)
