"""OpenSearch errors are classified by status and type, and request paths never repair inline.

A load run showed a ``NotFoundError`` for a missing speaker document (whose id contained the
characters ``503``) read as index corruption, which sent a request thread into close, open and
force-merge of the live speaker index: 503s for every concurrent reader and a merge that failed.
"""

from __future__ import annotations

from collections.abc import Collection
from collections.abc import Mapping
from typing import Any

import pytest
import urllib3
from opensearchpy import OpenSearch
from opensearchpy.connection import Connection
from opensearchpy.exceptions import NotFoundError
from opensearchpy.exceptions import TransportError

from app.services.opensearch_service import client as os_client
from app.services.opensearch_service import repair
from app.services.opensearch_service import speaker_read


def test_a_missing_document_is_never_index_corruption():
    error = NotFoundError(
        404, '{"_id":"3063d708-d503-4f1c-9b1e-0a1b2c3d4e5f","found":false}', {"found": False}
    )

    assert os_client._is_index_corruption_error(error) is False


@pytest.mark.parametrize(
    "error",
    [
        TransportError(
            400, "index_closed_exception", {"error": {"type": "index_closed_exception"}}
        ),
        TransportError(503, "service unavailable", {}),
    ],
)
def test_a_closed_index_or_bare_503_is_unavailability_not_corruption(error):
    assert os_client._is_index_corruption_error(error) is False


def test_a_shard_that_reports_it_is_already_closed_is_corruption():
    body = {
        "error": {
            "type": "search_phase_execution_exception",
            "root_cause": [{"type": "already_closed_exception", "reason": "engine closed"}],
        }
    }

    assert os_client._is_index_corruption_error(TransportError(503, "x", body)) is True


class _Indices:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def close(self, **kwargs: Any) -> None:
        self.calls.append("close")

    def open(self, **kwargs: Any) -> None:
        self.calls.append("open")

    def forcemerge(self, **kwargs: Any) -> None:
        self.calls.append("forcemerge")


class _MissingDocumentClient:
    def __init__(self) -> None:
        self.indices = _Indices()

    def get(self, **kwargs: Any) -> Any:
        raise NotFoundError(404, '{"_id":"3063d708-d503-4f1c","found":false}', {"found": False})


def test_a_missing_speaker_embedding_never_closes_or_merges_the_index(monkeypatch):
    fake = _MissingDocumentClient()
    monkeypatch.setattr(speaker_read._client, "opensearch_client", fake)
    monkeypatch.setattr(speaker_read, "get_active_speaker_index", lambda: "speakers")

    assert speaker_read.get_speaker_embedding("3063d708-d503-4f1c") is None
    assert fake.indices.calls == []


class _FakeRedis:
    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.keys:
            return False
        self.keys[key] = value
        return True


def test_self_heal_is_dispatched_once_per_cooldown(monkeypatch):
    from app.core import celery as celery_module
    from app.core import redis as redis_module

    sent: list[str] = []
    fake_redis = _FakeRedis()
    monkeypatch.setattr(redis_module, "get_request_redis", lambda: fake_redis)
    monkeypatch.setattr(
        celery_module.celery_app, "send_task", lambda name, *a, **k: sent.append(name)
    )

    assert repair.request_index_selfheal("speakers") is True
    assert repair.request_index_selfheal("speakers") is False
    assert sent == ["search_index_selfheal"]


def test_self_heal_is_never_requested_on_the_public_demo(monkeypatch):
    """A failed read on the demo must not publish: it has no worker and refuses every publish,
    so ``send_task`` would raise ``DemoPublishRefusedError`` out of a GET (#628)."""
    from app.core import celery as celery_module
    from app.core import redis as redis_module
    from app.core.config import settings

    sent: list[str] = []
    fake_redis = _FakeRedis()
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    # Both clients answer, so only the demo check can be what stops the publish.
    monkeypatch.setattr(redis_module, "get_request_redis", lambda: fake_redis)
    monkeypatch.setattr(redis_module, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(
        celery_module.celery_app, "send_task", lambda name, *a, **k: sent.append(name)
    )

    assert repair.request_index_selfheal("speakers") is False
    assert sent == []
    assert fake_redis.keys == {}, "the cooldown must not be spent on a heal never requested"


class _StubConnection(Connection):
    """A real Connection whose wire call applies the timeout exactly as the urllib3 one does."""

    def perform_request(
        self,
        method: str,
        url: str,
        params: Mapping[str, Any] | None = None,
        body: bytes | None = None,
        timeout: int | float | None = None,
        ignore: Collection[int] = (),
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        urllib3.Timeout(total=timeout)  # raises ValueError for a string such as "60s"
        return 200, {}, "{}"


def test_reopen_index_gives_the_transport_a_numeric_socket_timeout(monkeypatch):
    real = OpenSearch(hosts=[{"host": "localhost", "port": 9200}], connection_class=_StubConnection)
    monkeypatch.setattr(repair._client, "opensearch_client", real)
    monkeypatch.setattr(repair, "_verify_index_queryable", lambda index: True)
    monkeypatch.setattr(repair._client, "reset_knn_health_cache", lambda: None)

    assert repair.reopen_index("speakers") is True
