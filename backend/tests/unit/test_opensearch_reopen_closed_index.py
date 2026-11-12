"""A close/reopen repair must never leave an index closed, and a closed chunk index heals (#628).

A demo-bundle import was followed by every search answering 503 with ``already_closed_exception``
on the ``transcript_chunks`` shard until someone ran ``_close`` then ``_open`` by hand. The
repair strategy ran close and open in one ``try``: a close that raised (or an open that did)
skipped the other half, and the health check, seeing a closed index, deleted and re-embedded the
whole corpus instead of reopening it.
"""

from __future__ import annotations

from typing import Any

import pytest
from opensearchpy.exceptions import TransportError

from tests.unit.test_search_index_health_repair import _ann

INDEX = "transcript_chunks"


class _ClosableIndices:
    def __init__(self, parent: _ClosableOpenSearch) -> None:
        self._p = parent

    def exists(self, index: str) -> bool:
        return True

    def exists_alias(self, name: str) -> bool:
        return False

    def get_mapping(self, index: str) -> dict[str, Any]:
        return {index: {"mappings": {"properties": self._p.mappings[index]}}}

    def close(self, index: str) -> None:
        self._p.calls.append("close")
        self._p.is_closed = True
        if self._p.close_raises:
            raise TransportError(500, "close timed out")

    def open(self, index: str, **kwargs: Any) -> dict[str, Any]:
        self._p.calls.append("open")
        self._p.is_closed = False
        if self._p.recovering:
            self._p.is_recovering = True
        return {"acknowledged": True, "shards_acknowledged": not self._p.recovering}

    def delete(self, index: str, **kwargs: Any) -> None:
        self._p.calls.append("delete")


class _Cluster:
    def __init__(self, parent: _ClosableOpenSearch) -> None:
        self._p = parent

    def health(self, **kwargs: Any) -> dict[str, Any]:
        self._p.calls.append("health")
        self._p.is_recovering = False
        return {"status": "yellow", "timed_out": False}


class _ClosableOpenSearch:
    """Answers 503 ``index_closed_exception`` while closed, like the real shard."""

    def __init__(self, *, closed: bool = False, close_raises: bool = False) -> None:
        self.mappings: dict[str, dict[str, Any]] = {INDEX: _ann(384)}
        self.is_closed = closed
        self.close_raises = close_raises
        self.recovering = False
        self.is_recovering = False
        self.cluster = _Cluster(self)
        self.calls: list[str] = []
        self.indices = _ClosableIndices(self)

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.is_recovering:
            raise TransportError(503, "search_phase_execution_exception", {"error": "recovering"})
        if self.is_closed:
            raise TransportError(503, "index_closed_exception", {"error": "closed"})
        return {"hits": {"total": {"value": 1, "relation": "eq"}, "hits": []}}

    def count(self, index: str) -> dict[str, int]:
        return {"count": 5}


@pytest.fixture
def cluster(monkeypatch):
    from app.services.opensearch_service import client as os_client
    from app.services.search import index_health as health

    def install(fake: _ClosableOpenSearch) -> _ClosableOpenSearch:
        monkeypatch.setattr(os_client, "opensearch_client", fake)
        monkeypatch.setattr(health, "clear_corruption_notice", lambda _index: None)
        monkeypatch.setattr(health, "repair_is_supervised", lambda _index: False)
        os_client.reset_knn_health_cache()
        return fake

    return install


def test_a_close_that_raises_still_reopens_the_index(cluster):
    from app.services.opensearch_service.repair import reopen_index

    fake = cluster(_ClosableOpenSearch(close_raises=True))

    with pytest.raises(TransportError):
        reopen_index(INDEX)

    assert fake.calls == ["close", "open"]
    assert fake.is_closed is False


def test_a_closed_chunk_index_is_reopened_not_deleted_and_rebuilt(cluster):
    from app.services.search.index_health import check_and_repair_chunks_index

    fake = cluster(_ClosableOpenSearch(closed=True))

    repaired = check_and_repair_chunks_index()

    assert repaired == [INDEX]
    assert "delete" not in fake.calls
    assert fake.is_closed is False


def test_reopen_waits_for_index_health_before_probing(cluster):
    from app.services.search.index_health import check_and_repair_chunks_index

    fake = cluster(_ClosableOpenSearch(closed=True))
    fake.recovering = True

    repaired = check_and_repair_chunks_index()

    assert repaired == [INDEX]
    assert fake.calls.index("health") > fake.calls.index("open")
    assert "delete" not in fake.calls


def test_an_unconfirmed_reopen_never_escalates_to_a_rebuild(cluster, monkeypatch):
    from app.services.search.index_health import check_and_repair_chunks_index

    fake = cluster(_ClosableOpenSearch(closed=True))
    fake.recovering = True
    fake.cluster.health = lambda **kw: {"status": "red", "timed_out": True}
    rebuilds: list[str] = []

    def spy(*_a: Any, **_k: Any) -> bool:
        rebuilds.append("rebuild")
        return True

    monkeypatch.setattr("app.services.search.index_health.rebuild_chunks_index", spy)

    assert check_and_repair_chunks_index() == []
    assert rebuilds == []
    assert "delete" not in fake.calls
