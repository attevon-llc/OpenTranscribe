"""``reindex_task._check_and_recreate_stale_index`` deletes the whole chunks index.

The reindex that follows only repopulates the **calling user's** files, so every
other account's chunks are gone until each of them runs a reindex of their own.
That makes each of the three checks below a data-loss boundary, not a nicety.

The OpenSearch client is stubbed; no index is touched.
"""

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.services.search.indexing_service import _INDEX_VERSION
from app.tasks import reindex_task

_INDEX = settings.OPENSEARCH_CHUNKS_INDEX
_CLIENT_PATH = "app.services.opensearch_service.opensearch_client"
_BODY_PATH = "app.services.search.indexing_service._get_index_body_with_dimension"

_OLD_MAPPINGS: dict[str, Any] = {
    "_meta": {"version": _INDEX_VERSION - 1},
    "properties": {"embedding": {"type": "knn_vector", "dimension": 768}},
}


def _client(mappings: dict[str, Any]) -> MagicMock:
    """Stub client whose ``get_mapping`` returns ``mappings`` for the chunks index."""
    client = MagicMock()
    client.indices.exists.return_value = True
    client.indices.get_mapping.return_value = {_INDEX: {"mappings": mappings}}
    return client


@pytest.fixture
def new_index_body():
    """Patch the index-body builder and yield the mock, so its input is assertable."""
    with patch(_BODY_PATH, return_value={"mappings": {"built": True}}) as builder:
        yield builder


def test_index_without_meta_is_not_destroyed(new_index_body):
    """Defect: a missing ``_meta`` read as version 0, i.e. unconditionally stale.

    An index carrying no version block has unknown provenance — it may be current,
    it may hold every user's chunks. ``meta.get("version", 0)`` made it always
    older than ``_INDEX_VERSION`` and therefore always deleted.
    """
    client = _client({"properties": {"embedding": {"dimension": 768}}})

    with patch(_CLIENT_PATH, client):
        reindex_task._check_and_recreate_stale_index()

    assert client.indices.delete.call_count == 0
    assert new_index_body.call_count == 0


def test_unreadable_dimension_aborts_before_any_delete(new_index_body):
    """Defect: the embedding dimension defaulted to 384 when the mapping read missed.

    On a 768d deployment that silently produced a 384d index in which *every*
    subsequent write fails. A dimension that cannot be read is a reason to stop,
    not a reason to guess.
    """
    client = _client({"_meta": {"version": _INDEX_VERSION - 1}, "properties": {}})

    with patch(_CLIENT_PATH, client):
        reindex_task._check_and_recreate_stale_index()

    assert client.indices.delete.call_count == 0
    assert new_index_body.call_count == 0


def test_failed_create_restores_an_index_rather_than_leaving_none(new_index_body):
    """Defect: delete and create shared one ``except``, so a failed create left NO index.

    The deployment was then left with the chunks index simply absent — every
    search and every write failing — recorded as a single warning line. A create
    failure must be followed by restoring the mapping that was just read.
    """
    client = _client(dict(_OLD_MAPPINGS))
    client.indices.create.side_effect = [RuntimeError("mapping rejected"), None]

    with patch(_CLIENT_PATH, client):
        reindex_task._check_and_recreate_stale_index()

    assert client.indices.create.call_count == 2
    assert client.indices.create.call_args_list[-1].kwargs["body"] == {"mappings": _OLD_MAPPINGS}


def test_stale_index_is_recreated_at_the_mapped_dimension(new_index_body):
    """Control: a genuinely stale index with a readable dimension IS recreated.

    Without this, the three refusals above would also be satisfied by a function
    that never recreates anything, and the schema upgrade would quietly stop.
    """
    client = _client(dict(_OLD_MAPPINGS))

    with patch(_CLIENT_PATH, client):
        reindex_task._check_and_recreate_stale_index()

    assert new_index_body.call_args.args == (768,)
    assert client.indices.create.call_count == 1
