"""Per-user chat abuse controls (issue #52).

These guard cost and backend capacity, so the interesting cases are the failure
ones: what happens at the boundary, and what happens when Redis is unavailable.
The controlling rule is FAIL OPEN — a limiter outage must degrade to "unlimited",
never to "nobody can chat".
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock
from unittest.mock import patch

from app.services.chat import limits
from tests.helpers import does_not_raise


def _redis(**behaviour) -> MagicMock:
    client = MagicMock()
    for name, value in behaviour.items():
        if isinstance(value, Exception):
            getattr(client, name).side_effect = value
        else:
            getattr(client, name).return_value = value
    return client


# ---------------------------------------------------------------------------
# Hourly message ceiling
# ---------------------------------------------------------------------------


def test_first_message_of_the_hour_sets_an_expiry():
    """Without the EXPIRE the counter would never reset and lock the user out."""
    client = _redis(incr=1)
    with patch("app.core.redis.get_redis", return_value=client):
        allowed, retry_after = limits.check_hourly_limit(user_id=1, limit=120)

    assert allowed is True
    assert retry_after == 0
    client.expire.assert_called_once()
    assert client.expire.call_args[0][1] == 3600


def test_subsequent_messages_do_not_reset_the_window():
    client = _redis(incr=7)
    with patch("app.core.redis.get_redis", return_value=client):
        assert limits.check_hourly_limit(user_id=1, limit=120)[0] is True
    client.expire.assert_not_called()


def test_message_at_the_limit_is_allowed():
    client = _redis(incr=120)
    with patch("app.core.redis.get_redis", return_value=client):
        assert limits.check_hourly_limit(user_id=1, limit=120)[0] is True


def test_message_past_the_limit_is_refused_with_a_retry_after():
    client = _redis(incr=121)
    with patch("app.core.redis.get_redis", return_value=client):
        allowed, retry_after = limits.check_hourly_limit(user_id=1, limit=120)

    assert allowed is False
    # Retry-After points at the next window boundary, never zero or negative.
    assert 0 < retry_after <= 3600


def test_hourly_limit_fails_open_when_redis_is_down():
    """A limiter outage must not stop people using the product."""
    client = _redis(incr=ConnectionError("redis down"))
    with patch("app.core.redis.get_redis", return_value=client):
        allowed, retry_after = limits.check_hourly_limit(user_id=1, limit=120)

    assert allowed is True
    assert retry_after == 0


def test_hourly_limit_keys_are_per_user_and_per_hour():
    client = _redis(incr=1)
    with patch("app.core.redis.get_redis", return_value=client):
        limits.check_hourly_limit(user_id=1, limit=10)
        limits.check_hourly_limit(user_id=2, limit=10)

    key_one, key_two = (call[0][0] for call in client.incr.call_args_list)
    assert key_one != key_two
    assert ":1:" in key_one and ":2:" in key_two


# ---------------------------------------------------------------------------
# Concurrent-stream cap
# ---------------------------------------------------------------------------


def test_slot_is_granted_below_the_cap():
    client = _redis(zcard=0)
    with patch("app.core.redis.get_redis", return_value=client):
        slot = limits.acquire_stream_slot(user_id=1, max_concurrent=2)
    assert slot, "a slot below the cap must be granted and return its id"
    client.zadd.assert_called_once()
    # A leak guard TTL is mandatory: a dead process must not hold a slot forever.
    client.expire.assert_called_once()


def test_stale_slots_are_pruned_before_counting():
    """THE leak fix.

    The old implementation was one counter whose TTL was refreshed on every
    acquire, so a slot leaked by a died-mid-stream request never aged out for a
    user who kept chatting: their usable concurrency degraded 2 -> 1 -> 0 with
    no recovery short of deleting the key by hand. Pruning by age on each
    acquire is what bounds that.
    """
    client = _redis(zcard=0)
    with patch("app.core.redis.get_redis", return_value=client):
        limits.acquire_stream_slot(user_id=1, max_concurrent=2)
    client.zremrangebyscore.assert_called_once()
    key, low, high = client.zremrangebyscore.call_args[0]
    assert low == "-inf"
    assert high < time.time(), "the prune cutoff must be in the past"


def test_slot_below_the_cap_is_granted_with_one_already_held():
    client = _redis(zcard=1)
    with patch("app.core.redis.get_redis", return_value=client):
        assert limits.acquire_stream_slot(user_id=1, max_concurrent=2)


def test_slot_at_the_cap_is_refused():
    client = _redis(zcard=2)
    with patch("app.core.redis.get_redis", return_value=client):
        assert limits.acquire_stream_slot(user_id=1, max_concurrent=2) is None
    client.zadd.assert_not_called(), "a refused attempt must not take a slot"


def test_slot_acquisition_fails_open():
    """Chat must not go down because Redis did."""
    client = _redis(zcard=ConnectionError("redis down"))
    with patch("app.core.redis.get_redis", return_value=client):
        assert limits.acquire_stream_slot(user_id=1, max_concurrent=2)


def test_release_removes_only_its_own_slot():
    """Releasing by id is idempotent and cannot free someone else's stream.

    A bare decrement could: a double release (retry, double finally) would hand
    out a slot still legitimately held by another in-flight request.
    """
    client = _redis()
    with patch("app.core.redis.get_redis", return_value=client):
        limits.release_stream_slot(user_id=1, slot_id="slot-abc")
    client.zrem.assert_called_once()
    assert client.zrem.call_args[0][1] == "slot-abc"
    client.delete.assert_not_called()


def test_release_without_an_id_prunes_by_age_and_never_clears_the_key():
    """Older call sites must degrade to pruning, not blanket removal."""
    client = _redis()
    with patch("app.core.redis.get_redis", return_value=client):
        limits.release_stream_slot(user_id=1)
    client.zremrangebyscore.assert_called_once()
    client.delete.assert_not_called()
    client.zrem.assert_not_called()


def test_release_is_silent_when_redis_is_down():
    client = _redis(zrem=ConnectionError("redis down"))
    with patch("app.core.redis.get_redis", return_value=client):
        with does_not_raise("a Redis outage must not surface when releasing a stream slot"):
            limits.release_stream_slot(user_id=1, slot_id="slot-abc")

    # Assert the raising path actually ran. Without this the test also passes when
    # release_stream_slot never touches Redis at all, which would prove nothing about
    # containment (issue #431).
    client.zrem.assert_called_once()


# ---------------------------------------------------------------------------
# Cancel flags
# ---------------------------------------------------------------------------


def test_cancel_flag_is_written_with_a_ttl():
    """An unbounded flag would leak keys for every message ever cancelled."""
    client = _redis()
    with patch("app.core.redis.get_redis", return_value=client):
        limits.request_cancel("msg-uuid")

    key, ttl, value = client.setex.call_args[0]
    assert "msg-uuid" in key
    assert ttl == 600
    assert value == "1"


def test_is_cancelled_reflects_the_flag():
    with patch("app.core.redis.get_redis", return_value=_redis(get=b"1")):
        assert limits.is_cancelled("msg-uuid") is True
    with patch("app.core.redis.get_redis", return_value=_redis(get=None)):
        assert limits.is_cancelled("msg-uuid") is False


def test_is_cancelled_reports_false_when_redis_is_down():
    """Fail open: an unreachable Redis must not cancel everyone's streams."""
    client = _redis(get=ConnectionError("redis down"))
    with patch("app.core.redis.get_redis", return_value=client):
        assert limits.is_cancelled("msg-uuid") is False


def test_clear_cancel_is_contained():
    client = _redis(delete=ConnectionError("redis down"))
    with patch("app.core.redis.get_redis", return_value=client):
        with does_not_raise("a Redis outage must not surface when clearing a cancel flag"):
            limits.clear_cancel("msg-uuid")

    client.delete.assert_called_once()
