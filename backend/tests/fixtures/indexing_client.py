"""Bind one fake OpenSearch client for the whole chunk-indexing write path.

Every module of the write path (index admin, pipelines, bulk, orphans and the chunk, digest and
summary planes) reads ``app.services.opensearch_service.client.opensearch_client`` at call time,
so one replacement there reaches all of them.
"""

from __future__ import annotations

from typing import Any


def bind_indexing_client(monkeypatch: Any, client: Any) -> None:
    """Point every chunk-indexing module's OpenSearch client at ``client`` for one test."""
    monkeypatch.setattr("app.services.opensearch_service.client.opensearch_client", client)
