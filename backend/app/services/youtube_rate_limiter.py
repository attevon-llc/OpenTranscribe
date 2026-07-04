"""
YouTube download rate limiter and playlist staggering service.

Provides anti-bot detection measures for YouTube downloads:
- Playlist staggering: Progressive delays when dispatching videos
- User rate limiting: Redis-based per-user quotas
- Pre-download jitter: Random delays before downloads

All features degrade gracefully if Redis is unavailable.
"""

import logging
import random
from datetime import UTC
from datetime import datetime

from app.core.config import settings

logger = logging.getLogger(__name__)


def calculate_playlist_delays(video_count: int) -> list[int]:
    """Calculate staggered delays for playlist videos.

    Progressive delays with jitter to avoid simultaneous requests:
    - Video 1: 0s (immediate)
    - Video 2: 5-10s
    - Video 3: 10-20s
    - Video 4: 15-30s
    - Video 5+: 20-40s (capped at max)

    Args:
        video_count: Number of videos in playlist

    Returns:
        List of countdown delays in seconds (cumulative)
    """
    if not settings.YOUTUBE_PLAYLIST_STAGGER_ENABLED:
        return [0] * video_count

    min_delay = settings.YOUTUBE_PLAYLIST_STAGGER_MIN_SECONDS
    max_delay = settings.YOUTUBE_PLAYLIST_STAGGER_MAX_SECONDS
    increment = settings.YOUTUBE_PLAYLIST_STAGGER_INCREMENT

    delays = [0]  # First video dispatches immediately
    cumulative = 0

    for i in range(1, video_count):
        # Progressive delay: increases with video index
        base_delay = min(min_delay + (i * increment), max_delay)

        # Add jitter: -20% to +50% of base delay
        jitter_range = int(base_delay * 0.7)
        jitter = random.randint(-jitter_range // 3, jitter_range)  # noqa: S311  # nosec B311

        delay = max(min_delay, base_delay + jitter)
        cumulative += delay
        delays.append(cumulative)

    logger.info(
        f"Calculated stagger for {video_count} videos: "
        f"first=0s, last={cumulative}s (~{cumulative // 60}min)"
    )

    return delays


class YouTubeRateLimiter:
    """Redis-based rate limiter for YouTube downloads."""

    def __init__(self):
        self._redis = None

    @property
    def redis(self):
        """Lazy Redis connection."""
        if self._redis is None:
            try:
                import redis as sync_redis

                self._redis = sync_redis.Redis(
                    host=settings.REDIS_HOST,
                    port=int(settings.REDIS_PORT),
                    password=settings.REDIS_PASSWORD or None,
                    db=1,  # Use cache DB (separate from Celery broker)
                    decode_responses=True,
                    socket_timeout=2,
                    socket_connect_timeout=2,
                )
                self._redis.ping()
            except Exception as e:
                logger.warning(f"Redis rate limiter unavailable: {e}")
                self._redis = None
        return self._redis

    def check_rate_limit(self, user_id: int) -> tuple[bool, str]:
        """Check if user can queue another download.

        Returns:
            (allowed: bool, reason: str)
        """
        if not settings.YOUTUBE_USER_RATE_LIMIT_ENABLED:
            return (True, "")

        try:
            client = self.redis
            if client is None:
                # Graceful degradation: allow if Redis unavailable
                logger.warning("Rate limiter check failed: Redis unavailable. Allowing request.")
                return (True, "")

            now = datetime.now(UTC).timestamp()

            # Check hourly limit
            hour_key = f"youtube:ratelimit:hour:{user_id}"
            hour_ago = now - 3600

            # Remove expired entries
            client.zremrangebyscore(hour_key, 0, hour_ago)

            # Count current hour
            hourly_count = client.zcount(hour_key, hour_ago, now)

            if hourly_count >= settings.YOUTUBE_USER_RATE_LIMIT_PER_HOUR:
                return (
                    False,
                    f"Hourly limit exceeded ({settings.YOUTUBE_USER_RATE_LIMIT_PER_HOUR}/hour). "
                    f"Try again in a few minutes.",
                )

            # Check daily limit
            day_key = f"youtube:ratelimit:day:{user_id}"
            day_ago = now - 86400

            client.zremrangebyscore(day_key, 0, day_ago)
            daily_count = client.zcount(day_key, day_ago, now)

            if daily_count >= settings.YOUTUBE_USER_RATE_LIMIT_PER_DAY:
                return (
                    False,
                    f"Daily limit exceeded ({settings.YOUTUBE_USER_RATE_LIMIT_PER_DAY}/day). "
                    f"Try again tomorrow.",
                )

            return (True, "")

        except Exception as e:
            # Graceful degradation: allow if Redis check fails
            logger.warning(f"Rate limiter check failed: {e}. Allowing request.")
            return (True, "")

    def record_download(self, user_id: int) -> None:
        """Record a download attempt with timestamp."""
        if not settings.YOUTUBE_USER_RATE_LIMIT_ENABLED:
            return

        try:
            client = self.redis
            if client is None:
                return

            now = datetime.now(UTC).timestamp()

            # Add to hourly tracker
            hour_key = f"youtube:ratelimit:hour:{user_id}"
            client.zadd(hour_key, {str(now): now})
            client.expire(hour_key, 3600)  # Auto-expire after 1 hour

            # Add to daily tracker
            day_key = f"youtube:ratelimit:day:{user_id}"
            client.zadd(day_key, {str(now): now})
            client.expire(day_key, 86400)  # Auto-expire after 24 hours

        except Exception as e:
            logger.warning(f"Failed to record download: {e}")

    def get_remaining_quota(self, user_id: int) -> dict:
        """Get remaining downloads for user."""
        if not settings.YOUTUBE_USER_RATE_LIMIT_ENABLED:
            return {
                "hourly_remaining": -1,  # -1 = unlimited
                "daily_remaining": -1,
                "hourly_limit": -1,
                "daily_limit": -1,
            }

        try:
            client = self.redis
            if client is None:
                return {
                    "hourly_remaining": -1,
                    "daily_remaining": -1,
                    "hourly_limit": -1,
                    "daily_limit": -1,
                }

            now = datetime.now(UTC).timestamp()

            # Hourly quota
            hour_key = f"youtube:ratelimit:hour:{user_id}"
            hour_ago = now - 3600
            client.zremrangebyscore(hour_key, 0, hour_ago)
            hourly_count = client.zcount(hour_key, hour_ago, now)

            # Daily quota
            day_key = f"youtube:ratelimit:day:{user_id}"
            day_ago = now - 86400
            client.zremrangebyscore(day_key, 0, day_ago)
            daily_count = client.zcount(day_key, day_ago, now)

            return {
                "hourly_remaining": settings.YOUTUBE_USER_RATE_LIMIT_PER_HOUR - hourly_count,
                "daily_remaining": settings.YOUTUBE_USER_RATE_LIMIT_PER_DAY - daily_count,
                "hourly_limit": settings.YOUTUBE_USER_RATE_LIMIT_PER_HOUR,
                "daily_limit": settings.YOUTUBE_USER_RATE_LIMIT_PER_DAY,
            }
        except Exception as e:
            logger.warning(f"Failed to get quota: {e}")
            return {
                "hourly_remaining": -1,
                "daily_remaining": -1,
                "hourly_limit": -1,
                "daily_limit": -1,
            }


# Singleton instance
youtube_rate_limiter = YouTubeRateLimiter()
