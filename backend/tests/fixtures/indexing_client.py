"""Bind one fake OpenSearch client into every module of the chunk-indexing write path.

``TranscriptIndexingService`` is assembled from per-plane mixins and helper modules, and each
of them reads its own module-level ``opensearch_client`` (imported by name from
``app.services.opensearch_service``). A test that drives the whole write path with a fake
cluster must therefore replace that name in each one; replacing it in only some leaves the
rest talking to the real (or absent) client. This is the single list of those modules.
"""

from __future__ import annotations

from typing import Any

INDEXING_CLIENT_MODULES: tuple[str, ...] = (
    "app.services.search.index_admin",
    "app.services.search.pipelines",
    "app.services.search.indexing.bulk",
    "app.services.search.indexing.orphans",
    "app.services.search.indexing.chunk_plane",
    "app.services.search.indexing.digest_plane",
    "app.services.search.indexing.summary_plane",
)


def bind_indexing_client(monkeypatch: Any, client: Any) -> None:
    """Point every chunk-indexing module's ``opensearch_client`` at ``client`` for one test."""
    for module in INDEXING_CLIENT_MODULES:
        monkeypatch.setattr(f"{module}.opensearch_client", client)
