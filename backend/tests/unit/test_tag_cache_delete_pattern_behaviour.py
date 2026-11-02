"""Characterization of ``RedisCacheService.delete_pattern`` and ``invalidate_tags_global``.

Written before the tags backend rewrite, against a fake Redis client (no live Redis): the
literal-key fast path, the SCAN walk, the 500-key delete batches, the summed return count
and the swallow-and-return-0 error handling.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import redis_cache_service
from app.services.redis_cache_service import RedisCacheService


class _FakeRedis:
    def __init__(
        self,
        keys: list[str] | None = None,
        *,
        delete_fails_on: int | None = None,
        delete_returns: int | None = None,
        scan_fails: bool = False,
    ):
        self.delete_returns = delete_returns
        self.scan_fails = scan_fails
        self.keys = list(keys or [])
        self.scan_calls: list[dict[str, Any]] = []
        self.delete_calls: list[tuple[str, ...]] = []
        self.delete_fails_on = delete_fails_on

    def scan_iter(self, match=None, count=None):
        self.scan_calls.append({"match": match, "count": count})
        if self.scan_fails:
            raise ConnectionError("scan failed")
        yield from list(self.keys)

    def delete(self, *keys: str) -> int:
        self.delete_calls.append(keys)
        if self.delete_fails_on is not None and len(self.delete_calls) == self.delete_fails_on:
            raise ConnectionError("redis went away")
        return len(keys) if self.delete_returns is None else self.delete_returns


def _service(client: Any) -> RedisCacheService:
    svc = RedisCacheService()
    svc._redis = client
    return svc


def test_the_scan_batch_constant_is_500():
    assert redis_cache_service._SCAN_BATCH == 500


def test_no_client_returns_zero():
    svc = RedisCacheService()
    svc._unavailable_until = float("inf")

    assert svc.delete_pattern("cache:tags:*") == 0


@pytest.mark.parametrize("pattern", ["cache:tags:7", "cache:status:12"])
def test_a_literal_key_is_deleted_directly_without_a_scan(pattern):
    client = _FakeRedis(["ignored"])

    deleted = _service(client).delete_pattern(pattern)

    assert deleted == 1
    assert client.delete_calls == [(pattern,)]
    assert client.scan_calls == []


@pytest.mark.parametrize("token", ["*", "?", "["])
def test_each_glob_token_switches_to_the_scan_path(token):
    client = _FakeRedis(["a", "b"])

    deleted = _service(client).delete_pattern(f"cache:x{token}")

    assert deleted == 2
    assert client.scan_calls == [{"match": f"cache:x{token}", "count": 500}]
    assert client.delete_calls == [("a", "b")]


def test_no_matches_deletes_nothing_and_returns_zero():
    client = _FakeRedis([])

    assert _service(client).delete_pattern("cache:tags:*") == 0
    assert client.delete_calls == []


@pytest.mark.parametrize(
    ("total", "batches"),
    [(1, [1]), (499, [499]), (500, [500]), (501, [500, 1]), (1200, [500, 500, 200])],
)
def test_matches_are_deleted_in_batches_of_at_most_500_and_summed(total, batches):
    keys = [f"cache:tags:{i}" for i in range(total)]
    client = _FakeRedis(keys)

    deleted = _service(client).delete_pattern("cache:tags:*")

    assert deleted == total
    assert [len(call) for call in client.delete_calls] == batches
    assert [k for call in client.delete_calls for k in call] == keys


def test_the_return_is_what_redis_reports_deleted_not_the_keys_scanned():
    client = _FakeRedis(["a", "b", "c"], delete_returns=1)

    assert _service(client).delete_pattern("cache:*") == 1


def test_a_failure_mid_walk_is_swallowed_and_returns_zero_even_after_an_earlier_batch():
    keys = [f"k{i}" for i in range(501)]
    client = _FakeRedis(keys, delete_fails_on=2)

    deleted = _service(client).delete_pattern("cache:*")

    assert deleted == 0
    assert [len(call) for call in client.delete_calls] == [500, 1]


def test_a_failing_literal_delete_returns_zero():
    client = _FakeRedis(delete_fails_on=1)

    assert _service(client).delete_pattern("cache:tags:3") == 0


def test_a_failing_scan_returns_zero():
    client = _FakeRedis(scan_fails=True)

    assert _service(client).delete_pattern("cache:tags:*") == 0


def test_invalidate_tags_global_scans_every_users_tag_key_and_returns_the_count():
    client = _FakeRedis(["cache:tags:1", "cache:tags:2", "cache:tags:3"])

    count = _service(client).invalidate_tags_global()

    assert count == 3
    assert client.scan_calls == [{"match": "cache:tags:*", "count": 500}]
    assert client.delete_calls == [("cache:tags:1", "cache:tags:2", "cache:tags:3")]


def test_invalidate_tags_global_returns_zero_without_redis():
    svc = RedisCacheService()
    svc._unavailable_until = float("inf")

    assert svc.invalidate_tags_global() == 0
