"""A deployment-wide cap on open chat streams, beside the per-user one (audit H1).

The per-user cap (``chat.limits.max_concurrent_streams``, 6) bounds one person. Nothing
bounded the deployment, so enough users together could open more replies than the model
and the API could serve. ``CHAT_MAX_CONCURRENT_STREAMS_GLOBAL`` is held in one Redis
sorted set shared by every API process, keyed by the same slot id as the user's own.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

from redis.exceptions import ConnectionError as RedisConnectionError

from app.services.chat import limits

GLOBAL_KEY = "chat:active:global"


def _redis(**behaviour) -> MagicMock:
    client = MagicMock()
    for name, value in behaviour.items():
        if isinstance(value, Exception):
            getattr(client, name).side_effect = value
        else:
            getattr(client, name).return_value = value
    return client


def test_a_slot_below_the_cap_is_recorded_under_the_shared_key():
    client = _redis(zcard=63)
    with patch("app.core.redis.get_request_redis", return_value=client):
        assert limits.acquire_global_stream_slot("slot-1", max_concurrent=64) is True

    key, mapping = client.zadd.call_args.args
    assert key == GLOBAL_KEY
    assert list(mapping) == ["slot-1"]
    client.expire.assert_called_once()


def test_a_slot_at_the_cap_is_refused_and_not_recorded():
    client = _redis(zcard=64)
    with patch("app.core.redis.get_request_redis", return_value=client):
        assert limits.acquire_global_stream_slot("slot-1", max_concurrent=64) is False
    client.zadd.assert_not_called()


def test_stale_entries_are_pruned_before_counting():
    client = _redis(zcard=0)
    with patch("app.core.redis.get_request_redis", return_value=client):
        limits.acquire_global_stream_slot("slot-1", max_concurrent=64)
    key, low, _high = client.zremrangebyscore.call_args.args
    assert (key, low) == (GLOBAL_KEY, "-inf")


def test_a_zero_cap_means_no_cap_and_no_redis_call():
    client = _redis(zcard=10_000)
    with patch("app.core.redis.get_request_redis", return_value=client):
        assert limits.acquire_global_stream_slot("slot-1", max_concurrent=0) is True
    client.zcard.assert_not_called()


def test_the_cap_fails_open_when_redis_is_down():
    client = _redis(zremrangebyscore=RedisConnectionError("redis down"))
    with patch("app.core.redis.get_request_redis", return_value=client):
        assert limits.acquire_global_stream_slot("slot-1", max_concurrent=64) is True


def test_releasing_a_stream_frees_both_its_user_slot_and_its_global_slot():
    client = _redis()
    with patch("app.core.redis.get_request_redis", return_value=client):
        limits.release_stream_slot(7, "slot-1")

    removed = {call.args for call in client.zrem.call_args_list}
    assert removed == {("chat:active:7", "slot-1"), (GLOBAL_KEY, "slot-1")}
