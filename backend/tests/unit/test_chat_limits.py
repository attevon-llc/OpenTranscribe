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

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

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


# The check-and-update steps are one Redis script each (concurrency audit L2), so their
# behaviour is asserted against a real Redis (``local_model_redis``); a mock would only
# record that ``eval`` was called. Concurrency is in ``test_chat_limits_atomic.py``.


@pytest.fixture
def real_redis(local_model_redis):
    local_model_redis.flushdb()
    with patch("app.core.redis.get_request_redis", return_value=local_model_redis):
        yield local_model_redis
    local_model_redis.flushdb()


@pytest.mark.xdist_group("local_model_redis")
def test_first_message_of_the_hour_sets_an_expiry(real_redis):
    """Without the EXPIRE the counter would never reset and lock the user out."""
    allowed, retry_after = limits.check_hourly_limit(user_id=1, limit=120)

    assert allowed is True
    assert retry_after == 0
    (key,) = real_redis.keys("rl:chat:msg:1:*")
    assert 0 < real_redis.ttl(key) <= 3600


@pytest.mark.xdist_group("local_model_redis")
def test_subsequent_messages_do_not_reset_the_window(real_redis):
    limits.check_hourly_limit(user_id=1, limit=120)
    (key,) = real_redis.keys("rl:chat:msg:1:*")
    real_redis.expire(key, 1000)  # stands in for time having passed in the window

    assert limits.check_hourly_limit(user_id=1, limit=120)[0] is True
    assert real_redis.ttl(key) <= 1000


@pytest.mark.xdist_group("local_model_redis")
def test_message_at_the_limit_is_allowed_and_past_it_refused(real_redis):
    verdicts = [limits.check_hourly_limit(user_id=1, limit=3) for _ in range(4)]

    assert [allowed for allowed, _ in verdicts] == [True, True, True, False]
    # Retry-After points at the next window boundary, never zero or negative.
    assert 0 < verdicts[-1][1] <= 3600


def test_hourly_limit_fails_open_when_redis_is_down():
    """A limiter outage must not stop people using the product."""
    client = _redis(eval=RedisConnectionError("redis down"))
    with patch("app.core.redis.get_request_redis", return_value=client):
        allowed, retry_after = limits.check_hourly_limit(user_id=1, limit=120)

    assert allowed is True
    assert retry_after == 0
    client.eval.assert_called_once()


@pytest.mark.xdist_group("local_model_redis")
def test_hourly_limit_keys_are_per_user_and_per_hour(real_redis):
    limits.check_hourly_limit(user_id=1, limit=10)
    limits.check_hourly_limit(user_id=2, limit=10)

    keys = sorted(k.decode() for k in real_redis.keys("rl:chat:msg:*"))
    assert len(keys) == 2
    assert ":1:" in keys[0] and ":2:" in keys[1]
    assert keys[0].rsplit(":", 1)[1] == str(int(time.time()) // 3600)


# ---------------------------------------------------------------------------
# Concurrent-stream cap
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("local_model_redis")
def test_slot_is_granted_below_the_cap(real_redis):
    slot = limits.acquire_stream_slot(user_id=1, max_concurrent=2)
    assert slot, "a slot below the cap must be granted and return its id"
    assert real_redis.zscore("chat:active:1", slot) is not None
    # A leak guard TTL is mandatory: a dead process must not hold a slot forever.
    assert real_redis.ttl("chat:active:1") > 0


@pytest.mark.xdist_group("local_model_redis")
def test_stale_slots_are_pruned_before_counting(real_redis):
    """THE leak fix.

    The old implementation was one counter whose TTL was refreshed on every
    acquire, so a slot leaked by a died-mid-stream request never aged out for a
    user who kept chatting: their usable concurrency degraded 2 -> 1 -> 0 with
    no recovery short of deleting the key by hand. Pruning by age on each
    acquire is what bounds that.
    """
    leaked = time.time() - limits._ACTIVE_TTL_SECONDS - 1
    real_redis.zadd("chat:active:1", {"leaked-a": leaked, "leaked-b": leaked})

    slot = limits.acquire_stream_slot(user_id=1, max_concurrent=2)

    assert slot
    assert real_redis.zscore("chat:active:1", "leaked-a") is None


@pytest.mark.xdist_group("local_model_redis")
def test_slot_below_the_cap_is_granted_with_one_already_held(real_redis):
    assert limits.acquire_stream_slot(user_id=1, max_concurrent=2)
    assert limits.acquire_stream_slot(user_id=1, max_concurrent=2)


@pytest.mark.xdist_group("local_model_redis")
def test_slot_at_the_cap_is_refused(real_redis):
    for _ in range(2):
        assert limits.acquire_stream_slot(user_id=1, max_concurrent=2)

    assert limits.acquire_stream_slot(user_id=1, max_concurrent=2) is None
    assert real_redis.zcard("chat:active:1") == 2, "a refused attempt must not take a slot"


def test_slot_acquisition_fails_open():
    """Chat must not go down because Redis did."""
    client = _redis(type="zset", eval=RedisConnectionError("redis down"))
    with patch("app.core.redis.get_request_redis", return_value=client):
        assert limits.acquire_stream_slot(user_id=1, max_concurrent=2)
    client.eval.assert_called_once()


def test_release_removes_only_its_own_slot():
    """Releasing by id is idempotent and cannot free someone else's stream.

    A bare decrement could: a double release (retry, double finally) would hand
    out a slot still legitimately held by another in-flight request.
    """
    client = _redis()
    with patch("app.core.redis.get_request_redis", return_value=client):
        limits.release_stream_slot(user_id=1, slot_id="slot-abc")
    # Once from the user's set and once from the deployment-wide one, both by this id.
    assert [call.args[1] for call in client.zrem.call_args_list] == ["slot-abc", "slot-abc"]
    client.delete.assert_not_called()


def test_release_without_an_id_prunes_by_age_and_never_clears_the_key():
    """Older call sites must degrade to pruning, not blanket removal."""
    client = _redis()
    with patch("app.core.redis.get_request_redis", return_value=client):
        limits.release_stream_slot(user_id=1)
    client.zremrangebyscore.assert_called_once()
    client.delete.assert_not_called()
    client.zrem.assert_not_called()


def test_release_is_silent_when_redis_is_down():
    client = _redis(zrem=RedisConnectionError("redis down"))
    with patch("app.core.redis.get_request_redis", return_value=client):
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
    with patch("app.core.redis.get_request_redis", return_value=client):
        limits.request_cancel("msg-uuid")

    key, ttl, value = client.setex.call_args[0]
    assert "msg-uuid" in key
    assert ttl == 600
    assert value == "1"


def test_is_cancelled_reflects_the_flag():
    with patch("app.core.redis.get_request_redis", return_value=_redis(get=b"1")):
        assert limits.is_cancelled("msg-uuid") is True
    with patch("app.core.redis.get_request_redis", return_value=_redis(get=None)):
        assert limits.is_cancelled("msg-uuid") is False


def test_is_cancelled_reports_false_when_redis_is_down():
    """Fail open: an unreachable Redis must not cancel everyone's streams."""
    client = _redis(get=RedisConnectionError("redis down"))
    with patch("app.core.redis.get_request_redis", return_value=client):
        assert limits.is_cancelled("msg-uuid") is False


def test_clear_cancel_is_contained():
    client = _redis(delete=RedisConnectionError("redis down"))
    with patch("app.core.redis.get_request_redis", return_value=client):
        with does_not_raise("a Redis outage must not surface when clearing a cancel flag"):
            limits.clear_cancel("msg-uuid")

    client.delete.assert_called_once()


# --------------------- the hourly quota is a SPEND control (local ≠ remote)


class _Cfg:
    """Minimal stand-in for an LLM config: `is_local_provider` reads only these."""

    def __init__(self, provider: str, base_url: str = ""):
        self.provider = provider
        self.base_url = base_url


def test_a_local_provider_is_recognised_as_local():
    """The precondition for the endpoint's quota skip. Without this the skip
    below would be unreachable and its test would pass vacuously.

    `base_url` must be a real local endpoint — `is_local_provider` now keys
    `vllm`/`ollama` locality on it too (a `vllm` config can legitimately point
    at a hosted SaaS, and a bare provider name no longer decides it).
    """
    from app.services.redaction.llm_guard import is_local_provider

    assert is_local_provider(_Cfg("vllm", base_url="http://localhost:8012/v1")) is True
    assert is_local_provider(_Cfg("ollama", base_url="http://localhost:11434")) is True


def test_a_hosted_vllm_provider_is_not_local_so_the_quota_applies():
    """A `vllm`-provider config pointed at a hosted SaaS is a genuine

    third-party API call — the quota must still apply, the opposite of the
    self-hosted-GPU case above.
    """
    from unittest.mock import patch

    from app.services.redaction.llm_guard import is_local_provider

    with patch(
        "app.utils.url_validation.resolve_public_addresses",
        return_value=(["93.184.216.34"], ""),
    ):
        cfg = _Cfg("vllm", base_url="https://vllm.some-saas.example/v1")
        assert is_local_provider(cfg) is False


def test_a_remote_provider_is_not_local_so_the_quota_still_applies():
    """The control. A spend control must keep applying where there IS spend —
    and `is_local_provider` fails closed, so anything unrecognised reads remote.
    """
    from app.services.redaction.llm_guard import is_local_provider

    assert is_local_provider(_Cfg("openai")) is False
    assert is_local_provider(_Cfg("anthropic")) is False
    assert is_local_provider(_Cfg("openrouter")) is False


def test_the_endpoint_gates_the_hourly_check_on_provider_locality():
    """The quota check must be reached only for a non-local provider.

    Asserted against the endpoint SOURCE rather than by driving a stream: the
    handler needs a conversation, an LLM, Redis and a live scope to reach this
    line, and a test that mocked all four would be asserting on the mocks. What
    matters structurally is that `check_hourly_limit` sits under an
    `is_local_provider` guard and that the guard is negated -- an un-negated one
    would skip the quota for exactly the providers that bill.
    """
    import inspect
    import re

    from app.api.endpoints.chat import messages as messages_mod

    src = inspect.getsource(messages_mod)
    guard = re.search(
        r"if not is_local_provider\(llm\.config\):\s*\n\s*allowed, retry_after = "
        r"limits\.check_hourly_limit\(",
        src,
    )
    assert guard, "check_hourly_limit must sit under `if not is_local_provider(llm.config):`"
    # And it must appear exactly once -- a second, ungated call site would
    # reinstate the quota for local models while this test still passed.
    assert src.count("limits.check_hourly_limit(") == 1


def test_the_concurrency_slot_is_not_gated_on_locality():
    """max_concurrent_streams bounds GPU contention, which is just as real for a
    local model. Gating it the same way would be the plausible-looking mistake.
    """
    import inspect

    from app.api.endpoints.chat import messages as messages_mod

    src = inspect.getsource(messages_mod)
    acquire = src.index("limits.acquire_stream_slot(")
    preceding = src[max(0, acquire - 400) : acquire]
    assert "is_local_provider" not in preceding, (
        "the concurrency slot must stay unconditional -- it is not a spend control"
    )
