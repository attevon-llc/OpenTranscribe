"""Coded defaults for the DB-backed YouTube ingestion policy (``youtube.*`` SystemSettings)."""

DEFAULT_YOUTUBE_USER_RATE_LIMIT_ENABLED = True
DEFAULT_YOUTUBE_USER_RATE_LIMIT_PER_HOUR = 50
DEFAULT_YOUTUBE_USER_RATE_LIMIT_PER_DAY = 500
DEFAULT_YOUTUBE_AUTO_RETRY_ENABLED = True
#: Re-queued per recovery cycle (every 10 minutes); keep well below the hourly limit / 6.
DEFAULT_YOUTUBE_RECOVERY_BATCH_SIZE = 3

#: Inclusive bounds enforced on write and on seed.
YOUTUBE_RATE_LIMIT_MIN = 1
YOUTUBE_RATE_LIMIT_MAX = 100_000
YOUTUBE_RECOVERY_BATCH_MIN = 1
YOUTUBE_RECOVERY_BATCH_MAX = 100
