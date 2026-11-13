"""Shared in-memory Redis fakes: model only the commands a test needs."""


class RedisDownExceptFor:
    """Base for fakes: every command the subclass does not define behaves like an unreachable Redis.

    Callers then take their documented outage fallback (they catch ``redis.RedisError``) instead of
    failing with an AttributeError on an unmodelled command such as ``zrem``.
    """

    def __getattr__(self, name):
        from redis.exceptions import ConnectionError as RedisConnectionError

        def _down(*_args, **_kwargs):
            raise RedisConnectionError(f"fake redis: {name} unavailable")

        return _down
