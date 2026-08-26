"""``youtube_rate_limiter`` — playlist staggering and the per-user download quota.

``calculate_playlist_delays`` runs everywhere (pure arithmetic). The
``YouTubeRateLimiter`` sliding-window tests run against a REAL Redis
(``localhost:5177``, db 1 — the same db the module itself uses) rather than a
stand-in: the whole point is the zset arithmetic (``zremrangebyscore`` +
``zcount``), and an in-process fake cannot tell a real off-by-one in that
arithmetic from a correct implementation. They skip if the dev stack's Redis
is not reachable/authenticated, matching
``tests/unit/test_document_shard_progress.py``'s convention.

``GET /files/youtube/quota`` documents ``-1`` as meaning "unlimited"
(``app/api/CLAUDE.md``). ``TestSlidingWindowAgainstRealRedis`` includes a
regression for the collision this created: a user merely OVER quota (reachable
via the check-then-record race between ``check_rate_limit`` and
``record_download``, which are two independent Redis round trips) read back as
"unlimited" too, because ``get_remaining_quota`` computed ``limit - count``
with no floor.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import youtube_rate_limiter as yrl

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_REDIS_HOST = "127.0.0.1"
_REDIS_PORT = 5177


def _load_redis_password() -> str | None:
    """Read ``REDIS_PASSWORD`` from the repo ``.env``, the same way conftest reads
    DB/MinIO creds — used only to open a connection, never logged or asserted on."""
    env_file = _PROJECT_ROOT / ".env"
    if not env_file.exists():
        return None
    from dotenv import dotenv_values

    return dotenv_values(env_file).get("REDIS_PASSWORD") or None


_REDIS_PASSWORD = _load_redis_password()


def _redis_usable() -> bool:
    try:
        import redis as redis_mod

        client = redis_mod.Redis(
            host=_REDIS_HOST,
            port=_REDIS_PORT,
            password=_REDIS_PASSWORD,
            db=1,
            socket_timeout=1,
            socket_connect_timeout=1,
            decode_responses=True,
        )
        return bool(client.ping())
    except Exception:
        return False


class TestCalculatePlaylistDelays:
    def test_disabled_returns_zero_delay_for_every_video(self, monkeypatch):
        monkeypatch.setattr(settings, "YOUTUBE_PLAYLIST_STAGGER_ENABLED", False)

        assert yrl.calculate_playlist_delays(5) == [0, 0, 0, 0, 0]

    def test_a_single_video_dispatches_immediately(self, monkeypatch):
        monkeypatch.setattr(settings, "YOUTUBE_PLAYLIST_STAGGER_ENABLED", True)

        assert yrl.calculate_playlist_delays(1) == [0]

    def test_delays_follow_the_progressive_formula_with_jitter_neutralised(self, monkeypatch):
        """Pin the exact schedule so a change to the formula shows up as a diff,
        not just "still increasing"."""
        monkeypatch.setattr(settings, "YOUTUBE_PLAYLIST_STAGGER_ENABLED", True)
        monkeypatch.setattr(settings, "YOUTUBE_PLAYLIST_STAGGER_MIN_SECONDS", 5)
        monkeypatch.setattr(settings, "YOUTUBE_PLAYLIST_STAGGER_MAX_SECONDS", 30)
        monkeypatch.setattr(settings, "YOUTUBE_PLAYLIST_STAGGER_INCREMENT", 5)
        monkeypatch.setattr(yrl.random, "randint", lambda _a, _b: 0)

        delays = yrl.calculate_playlist_delays(6)

        # video1=0, then base_delay=min+i*increment capped at max, cumulative-summed:
        # i=1:10 i=2:15 i=3:20 i=4:25 i=5:30(capped) -> cumulative 10,25,45,70,100
        assert delays == [0, 10, 25, 45, 70, 100]

    def test_the_per_video_delay_is_capped_at_the_configured_maximum(self, monkeypatch):
        monkeypatch.setattr(settings, "YOUTUBE_PLAYLIST_STAGGER_ENABLED", True)
        monkeypatch.setattr(settings, "YOUTUBE_PLAYLIST_STAGGER_MIN_SECONDS", 5)
        monkeypatch.setattr(settings, "YOUTUBE_PLAYLIST_STAGGER_MAX_SECONDS", 10)
        monkeypatch.setattr(settings, "YOUTUBE_PLAYLIST_STAGGER_INCREMENT", 5)
        monkeypatch.setattr(yrl.random, "randint", lambda _a, _b: 0)

        delays = yrl.calculate_playlist_delays(4)

        # Every per-video delay after the first is capped at max=10, uncapped it
        # would grow by 5 each step (10, 15, 20) — the cap must hold it flat.
        per_video = [b - a for a, b in zip(delays, delays[1:], strict=False)]
        assert per_video == [10, 10, 10]

    def test_cumulative_delays_strictly_increase_with_real_jitter(self, monkeypatch):
        """Negative control for the two pinned tests above: with real randomness
        (no jitter patch) the schedule must still never go backwards or repeat."""
        monkeypatch.setattr(settings, "YOUTUBE_PLAYLIST_STAGGER_ENABLED", True)
        monkeypatch.setattr(settings, "YOUTUBE_PLAYLIST_STAGGER_MIN_SECONDS", 5)
        monkeypatch.setattr(settings, "YOUTUBE_PLAYLIST_STAGGER_MAX_SECONDS", 40)
        monkeypatch.setattr(settings, "YOUTUBE_PLAYLIST_STAGGER_INCREMENT", 5)

        delays = yrl.calculate_playlist_delays(10)

        assert delays[0] == 0
        assert len(delays) == 10
        for prev, nxt in zip(delays, delays[1:], strict=False):
            assert nxt > prev


class TestGracefulDegradation:
    """The two documented "fail open" paths, exercised without touching a real Redis."""

    def test_a_disabled_feature_never_even_looks_at_redis(self, monkeypatch):
        monkeypatch.setattr(settings, "YOUTUBE_USER_RATE_LIMIT_ENABLED", False)
        limiter = yrl.YouTubeRateLimiter()

        def _poisoned(_self):
            raise AssertionError("redis property must not be read while the feature is disabled")

        monkeypatch.setattr(type(limiter), "redis", property(_poisoned))

        assert limiter.check_rate_limit(123) == (True, "")
        limiter.record_download(123)  # must be a no-op, not raise
        assert limiter.get_remaining_quota(123) == {
            "hourly_remaining": -1,
            "daily_remaining": -1,
            "hourly_limit": -1,
            "daily_limit": -1,
        }

    def test_an_unreachable_redis_allows_the_request(self, monkeypatch):
        monkeypatch.setattr(settings, "YOUTUBE_USER_RATE_LIMIT_ENABLED", True)
        limiter = yrl.YouTubeRateLimiter()
        monkeypatch.setattr(type(limiter), "redis", property(lambda self: None))

        assert limiter.check_rate_limit(123) == (True, "")

    def test_an_unreachable_redis_reports_the_unlimited_sentinel(self, monkeypatch):
        monkeypatch.setattr(settings, "YOUTUBE_USER_RATE_LIMIT_ENABLED", True)
        limiter = yrl.YouTubeRateLimiter()
        monkeypatch.setattr(type(limiter), "redis", property(lambda self: None))

        quota = limiter.get_remaining_quota(123)

        assert quota == {
            "hourly_remaining": -1,
            "daily_remaining": -1,
            "hourly_limit": -1,
            "daily_limit": -1,
        }


@pytest.mark.skipif(
    not _redis_usable(),
    reason=(
        "Real Redis (127.0.0.1:5177, db=1 -- the dev stack's cache db) is not "
        "reachable/authenticated from this process. These tests exercise the actual "
        "sliding-window zset arithmetic (zremrangebyscore + zcount) against a real "
        "Redis rather than a stand-in, matching this repo's 'run against the real "
        "service, don't silently skip' convention "
        "(tests/unit/test_document_shard_progress.py is the sibling pattern). "
        "Start the dev stack: ./opentr.sh start dev"
    ),
)
class TestSlidingWindowAgainstRealRedis:
    @pytest.fixture
    def limiter_env(self, monkeypatch):
        monkeypatch.setattr(settings, "REDIS_HOST", _REDIS_HOST)
        monkeypatch.setattr(settings, "REDIS_PORT", str(_REDIS_PORT))
        monkeypatch.setattr(settings, "REDIS_PASSWORD", _REDIS_PASSWORD or "")
        monkeypatch.setattr(settings, "YOUTUBE_USER_RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(settings, "YOUTUBE_USER_RATE_LIMIT_PER_HOUR", 3)
        monkeypatch.setattr(settings, "YOUTUBE_USER_RATE_LIMIT_PER_DAY", 5)

        limiter = yrl.YouTubeRateLimiter()
        # A synthetic NEGATIVE user id: real user ids are positive auto-increment
        # Postgres PKs, so this can never collide with a real account's counters
        # in the shared dev-stack Redis.
        import random

        user_id = -random.randint(10**6, 2**31 - 1)  # noqa: S311  # nosec B311 - test id, not a secret

        yield limiter, user_id

        client = limiter.redis
        if client is not None:
            client.delete(f"youtube:ratelimit:hour:{user_id}")
            client.delete(f"youtube:ratelimit:day:{user_id}")

    def test_allows_requests_below_the_hourly_limit(self, limiter_env):
        limiter, user_id = limiter_env
        limiter.record_download(user_id)
        limiter.record_download(user_id)  # 2 of 3

        assert limiter.check_rate_limit(user_id) == (True, "")

    def test_blocks_exactly_at_the_hourly_limit_boundary(self, limiter_env):
        limiter, user_id = limiter_env
        for _ in range(3):  # exactly the configured limit
            limiter.record_download(user_id)

        allowed, reason = limiter.check_rate_limit(user_id)

        assert allowed is False
        assert "Hourly limit exceeded" in reason

    def test_daily_limit_blocks_even_with_hourly_headroom(self, limiter_env, monkeypatch):
        limiter, user_id = limiter_env
        monkeypatch.setattr(settings, "YOUTUBE_USER_RATE_LIMIT_PER_HOUR", 1000)
        for _ in range(5):  # the configured daily limit
            limiter.record_download(user_id)

        allowed, reason = limiter.check_rate_limit(user_id)

        assert allowed is False
        assert "Daily limit exceeded" in reason

    def test_entries_older_than_the_window_are_pruned_and_excluded(self, limiter_env):
        limiter, user_id = limiter_env
        client = limiter.redis
        assert client is not None
        hour_key = f"youtube:ratelimit:hour:{user_id}"
        stale_ts = time.time() - 3700  # 1h + 100s old: outside the hourly window
        client.zadd(hour_key, {f"stale-{stale_ts}": stale_ts})

        # Fill right up to (but not over) the small limit with FRESH downloads.
        # If the stale entry above still counted, this would incorrectly block.
        limiter.record_download(user_id)
        limiter.record_download(user_id)

        allowed, _ = limiter.check_rate_limit(user_id)

        assert allowed is True
        # And check_rate_limit's own zremrangebyscore must have swept it out.
        assert client.zscore(hour_key, f"stale-{stale_ts}") is None

    def test_record_download_sets_a_bounded_ttl_on_both_windows(self, limiter_env):
        limiter, user_id = limiter_env
        limiter.record_download(user_id)
        client = limiter.redis
        assert client is not None

        hour_ttl = client.ttl(f"youtube:ratelimit:hour:{user_id}")
        day_ttl = client.ttl(f"youtube:ratelimit:day:{user_id}")

        assert 0 < hour_ttl <= 3600
        assert 0 < day_ttl <= 86400

    def test_disabled_record_download_leaves_no_trace(self, limiter_env, monkeypatch):
        limiter, user_id = limiter_env
        monkeypatch.setattr(settings, "YOUTUBE_USER_RATE_LIMIT_ENABLED", False)

        limiter.record_download(user_id)

        client = limiter.redis
        assert client is not None
        assert client.exists(f"youtube:ratelimit:hour:{user_id}") == 0

    def test_get_remaining_quota_reflects_recorded_downloads(self, limiter_env):
        limiter, user_id = limiter_env
        limiter.record_download(user_id)

        quota = limiter.get_remaining_quota(user_id)

        assert quota == {
            "hourly_remaining": 2,  # limit 3 - 1 recorded
            "daily_remaining": 4,  # limit 5 - 1 recorded
            "hourly_limit": 3,
            "daily_limit": 5,
        }

    def test_get_remaining_quota_never_returns_the_unlimited_sentinel_for_a_real_user(
        self, limiter_env
    ):
        """Regression: a merely-OVER-quota user used to read back as "unlimited".

        ``GET /files/youtube/quota`` documents -1 as meaning "unlimited"
        (``app/api/CLAUDE.md``). ``check_rate_limit`` and ``record_download`` are two
        independent Redis round trips, so a burst of concurrent requests can each
        read a count below the limit, pass, and all record -- landing the real count
        one (or more) over the configured ceiling. ``get_remaining_quota`` computed
        ``limit - count`` with no floor, so landing exactly ONE over produced
        ``hourly_remaining == -1``: byte-identical to the documented "unlimited"
        sentinel, for a user who is in fact over quota.
        """
        limiter, user_id = limiter_env
        client = limiter.redis
        assert client is not None
        now = time.time()
        hour_key = f"youtube:ratelimit:hour:{user_id}"
        day_key = f"youtube:ratelimit:day:{user_id}"
        # 4 entries against a limit of 3, 6 against a limit of 5 -- one over each,
        # written directly rather than via record_download to simulate exactly what
        # a check-then-record race produces (the count exceeding the limit at all).
        for i in range(4):
            client.zadd(hour_key, {f"race-hour-{i}-{now}": now})
        for i in range(6):
            client.zadd(day_key, {f"race-day-{i}-{now}": now})

        quota = limiter.get_remaining_quota(user_id)

        assert quota["hourly_remaining"] != -1
        assert quota["daily_remaining"] != -1
        assert quota["hourly_remaining"] == 0
        assert quota["daily_remaining"] == 0
