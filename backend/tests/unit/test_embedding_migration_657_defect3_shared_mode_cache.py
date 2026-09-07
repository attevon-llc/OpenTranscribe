"""Cross-process embedding-mode cache invalidation (issue #657, defect 3).

Before this fix, ``EmbeddingModeService._cached_mode`` was a plain classvar,
cleared only inside ``finalize_v4_migration_task``'s ``finally`` block — on
whichever single worker process ran finalize. Every OTHER API process and
Celery worker kept a stale in-memory value until it happened to restart,
writing at the wrong embedding dimension in the meantime.

The fix moves the cache into Redis (shared by every process) with a short
TTL as a self-healing fallback. These tests simulate "another process" by
resetting the in-process fallback (``_local_cache``) to ``None`` — the state
a brand new worker starts in — and proving the mode still resolves correctly
from the shared store, with no OpenSearch round trip.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.embedding_mode_service import MODE_V3
from app.services.embedding_mode_service import MODE_V4
from app.services.embedding_mode_service import EmbeddingModeService


class _FakeRedisNetwork:
    """A dict shared across "processes" — stands in for the real Redis server."""

    def __init__(self):
        self.store: dict[str, bytes] = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, _ttl, value):
        self.store[key] = value.encode() if isinstance(value, str) else value

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture(autouse=True)
def _reset_local_cache():
    EmbeddingModeService._local_cache = None
    yield
    EmbeddingModeService._local_cache = None


@pytest.mark.unit
class TestSharedModeCacheVisibleAcrossProcesses:
    def test_mode_set_by_one_process_is_visible_to_another_without_opensearch(self, monkeypatch):
        network = _FakeRedisNetwork()
        monkeypatch.setattr("app.core.redis.get_redis", lambda: network)

        # "Process A" (e.g. the worker that ran finalize) sets the mode.
        EmbeddingModeService._set_cached_mode(MODE_V4)

        # "Process B": no local cache at all (a fresh process), but shares
        # the same Redis. It must read v4 without ever touching OpenSearch.
        EmbeddingModeService._local_cache = None
        opensearch_client = MagicMock()
        monkeypatch.setattr(
            "app.services.opensearch_service.get_opensearch_client",
            lambda: opensearch_client,
        )

        mode = EmbeddingModeService.detect_mode()

        assert mode == MODE_V4
        opensearch_client.indices.get_mapping.assert_not_called()
        opensearch_client.indices.exists.assert_not_called()

    def test_clear_cache_deletes_the_shared_key_not_just_the_local_one(self, monkeypatch):
        network = _FakeRedisNetwork()
        monkeypatch.setattr("app.core.redis.get_redis", lambda: network)

        EmbeddingModeService._set_cached_mode(MODE_V3)
        assert network.store  # the shared store actually holds something

        EmbeddingModeService.clear_cache()

        assert network.store == {}
        # And the local fallback used by other processes is unset too.
        assert EmbeddingModeService._get_cached_mode() is None

    def test_redis_outage_falls_back_to_local_cache_within_ttl(self, monkeypatch):
        def _broken_redis():
            raise ConnectionError("redis down")

        monkeypatch.setattr("app.core.redis.get_redis", _broken_redis)

        EmbeddingModeService._set_cached_mode(MODE_V4)  # Redis write fails, local succeeds
        assert EmbeddingModeService._get_cached_mode() == MODE_V4
