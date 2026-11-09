"""A revocation epoch stamped while Redis was unreachable reaches Redis once it is back.

Without the backlog, a logout-all (or password reset, admin termination) during a Redis
outage was enforced only by the replica that stamped it, from the database; every other
replica read Redis, found no epoch, and accepted the user's access tokens until they
expired. The replay runs through a Lua script that never lowers a newer epoch, which only a
real Redis can prove, so these tests use the throwaway Redis fixture.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.auth import token_service as token_service_module
from app.auth.revocation_backlog import PendingEpochs
from app.auth.session import InMemoryStore
from app.auth.token_service import USER_REVOCATION_EPOCH_PREFIX
from app.auth.token_service import TokenService
from app.utils import websocket_notify

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("local_model_redis")]

USER_UUID = "019ec90a-1b2c-7def-8000-0000000000e1"


class _DownStore:
    """A Redis client whose server has gone away."""

    def set(self, *_a: Any, **_k: Any) -> None:
        raise ConnectionError("redis down")

    def eval(self, *_a: Any, **_k: Any) -> None:
        raise ConnectionError("redis down")

    def publish(self, *_a: Any, **_k: Any) -> None:
        raise RedisConnectionError("redis down")


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _no_fanout(monkeypatch):
    monkeypatch.setattr(websocket_notify, "get_request_redis", lambda: _DownStore())


@pytest.fixture
def key(lm_namespace) -> str:
    return f"{lm_namespace}:{USER_REVOCATION_EPOCH_PREFIX}{USER_UUID}"


def test_flush_writes_the_epoch_with_its_remaining_lifetime(local_model_redis, key):
    clock = _Clock(10_000.0)
    backlog = PendingEpochs(clock=clock)
    backlog.remember(key, 9_990, ttl_seconds=600)
    clock.now += 100

    assert backlog.flush(local_model_redis) == 1

    assert int(local_model_redis.get(key)) == 9_990
    assert 490 <= local_model_redis.ttl(key) <= 500
    assert len(backlog) == 0


def test_flush_never_lowers_a_newer_epoch(local_model_redis, key):
    """Another replica stamped a later logout-all after the outage; it must stand."""
    local_model_redis.set(key, "9999", ex=600)
    backlog = PendingEpochs()
    backlog.remember(key, 9_990, ttl_seconds=600)

    backlog.flush(local_model_redis)

    assert int(local_model_redis.get(key)) == 9_999
    assert len(backlog) == 0


def test_flush_raises_an_older_epoch(local_model_redis, key):
    local_model_redis.set(key, "100", ex=600)
    backlog = PendingEpochs()
    backlog.remember(key, 9_990, ttl_seconds=600)

    backlog.flush(local_model_redis)

    assert int(local_model_redis.get(key)) == 9_990


def test_an_epoch_whose_tokens_have_all_expired_is_dropped_unwritten(local_model_redis, key):
    clock = _Clock(10_000.0)
    backlog = PendingEpochs(clock=clock)
    backlog.remember(key, 9_990, ttl_seconds=60)
    clock.now += 61

    assert backlog.flush(local_model_redis) == 0

    assert local_model_redis.get(key) is None
    assert len(backlog) == 0


def test_a_failed_flush_keeps_the_epoch_for_next_time(key):
    backlog = PendingEpochs()
    backlog.remember(key, 9_990, ttl_seconds=600)

    assert backlog.flush(_DownStore()) == 0

    assert len(backlog) == 1


def test_remember_keeps_the_newest_epoch_per_key(local_model_redis, key):
    backlog = PendingEpochs()
    backlog.remember(key, 9_995, ttl_seconds=600)
    backlog.remember(key, 9_990, ttl_seconds=600)

    backlog.flush(local_model_redis)

    assert int(local_model_redis.get(key)) == 9_995


def _real_key(namespace: str) -> str:
    return f"{USER_REVOCATION_EPOCH_PREFIX}{namespace}-{USER_UUID}"


def test_an_epoch_that_failed_to_stamp_is_replayed_when_redis_recovers(
    local_model_redis, lm_namespace, monkeypatch
):
    service = TokenService()
    service._store = _DownStore()
    user_uuid = f"{lm_namespace}-{USER_UUID}"
    before = int(time.time())

    assert service.stamp_user_revocation_epoch(user_uuid) is False
    assert service.degraded

    monkeypatch.setattr(token_service_module, "get_redis_client", lambda: local_model_redis)
    service._last_redis_probe = float("-inf")
    service._reprobe_redis()

    assert not service.degraded
    assert before <= int(local_model_redis.get(_real_key(lm_namespace)) or 0) <= time.time()


def test_an_epoch_stamped_on_the_in_memory_fallback_is_replayed_too(
    local_model_redis, lm_namespace, monkeypatch
):
    """Degraded since startup: the write 'succeeds' into a per-process store nobody else reads."""
    service = TokenService()
    service._store = InMemoryStore()
    service._degraded = True
    user_uuid = f"{lm_namespace}-{USER_UUID}"
    monkeypatch.setattr(token_service_module, "get_redis_client", lambda: None)
    before = int(time.time())

    service.stamp_user_revocation_epoch(user_uuid)

    monkeypatch.setattr(token_service_module, "get_redis_client", lambda: local_model_redis)
    service._last_redis_probe = float("-inf")
    service._reprobe_redis()

    assert before <= int(local_model_redis.get(_real_key(lm_namespace)) or 0) <= time.time()


def test_a_healthy_stamp_also_flushes_what_was_waiting(local_model_redis, lm_namespace):
    service = TokenService()
    service._store = local_model_redis
    waiting = f"{USER_REVOCATION_EPOCH_PREFIX}{lm_namespace}-waiting"
    service._pending_epochs.remember(waiting, 9_990, ttl_seconds=600)

    assert service.stamp_user_revocation_epoch(f"{lm_namespace}-{USER_UUID}") is True

    assert int(local_model_redis.get(waiting)) == 9_990
    assert int(local_model_redis.get(_real_key(lm_namespace)) or 0) > 9_990
