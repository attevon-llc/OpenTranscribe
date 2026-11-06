"""Search modules must see an OpenSearch client that is connected after they were imported."""

import importlib
import sys

import pytest

from app.services.opensearch_service import client as os_client

MODULES = [
    "app.services.search.indexing.bulk",
    "app.services.search.indexing.chunk_plane",
    "app.services.search.indexing.digest_plane",
    "app.services.search.indexing.summary_plane",
    "app.services.search.indexing.orphans",
    "app.services.search.pipelines",
    "app.services.search.index_admin",
    "app.services.search.suggestions",
]


class _Marker:
    """Stand-in client; only identity matters."""


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imported_while_client_is_none_sees_later_client(module_name, monkeypatch):
    monkeypatch.setattr(os_client, "opensearch_client", None)
    sys.modules.pop(module_name, None)
    try:
        module = importlib.import_module(module_name)
        marker = _Marker()
        monkeypatch.setattr(os_client, "opensearch_client", marker)
        assert module._client.opensearch_client is marker
    finally:
        sys.modules.pop(module_name, None)
        importlib.import_module(module_name)
