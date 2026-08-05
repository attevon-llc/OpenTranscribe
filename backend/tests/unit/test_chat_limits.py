"""Per-user chat abuse controls (issue #52).

These guard cost and backend capacity, so the interesting cases are the failure
ones: what happens at the boundary, and what happens when Redis is unavailable.
The controlling rule is FAIL OPEN — a limiter outage must degrade to "unlimited",
never to "nobody can chat".
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

from app.services.chat import limits


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
    client = _redis(incr=1)
    with patch("app.core.redis.get_redis", return_value=client):
        assert limits.acquire_stream_slot(user_id=1, max_concurrent=2) is True
    # A leak guard TTL is mandatory: a dead process must not hold a slot forever.
    client.expire.assert_called_once()


def test_slot_at_the_cap_is_still_granted():
    client = _redis(incr=2)
    with patch("app.core.redis.get_redis", return_value=client):
        assert limits.acquire_stream_slot(user_id=1, max_concurrent=2) is True


def test_slot_past_the_cap_is_refused_and_handed_back():
    """The refused attempt must not leave the counter inflated."""
    client = _redis(incr=3)
    with patch("app.core.redis.get_redis", return_value=client):
        assert limits.acquire_stream_slot(user_id=1, max_concurrent=2) is False
    client.decr.assert_called_once()


def test_slot_acquisition_fails_open():
    client = _redis(incr=ConnectionError("redis down"))
    with patch("app.core.redis.get_redis", return_value=client):
        assert limits.acquire_stream_slot(user_id=1, max_concurrent=2) is True


def test_releasing_never_drives_the_counter_negative():
    """Over-releasing (double finally, retry) must not create free slots."""
    client = _redis(decr=-1)
    with patch("app.core.redis.get_redis", return_value=client):
        limits.release_stream_slot(user_id=1)
    client.set.assert_called_once_with(client.set.call_args[0][0], 0)


def test_release_is_silent_when_redis_is_down():
    client = _redis(decr=ConnectionError("redis down"))
    with patch("app.core.redis.get_redis", return_value=client):
        limits.release_stream_slot(user_id=1)  # must not raise


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
        limits.clear_cancel("msg-uuid")  # must not raise
