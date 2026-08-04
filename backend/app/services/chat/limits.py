"""Per-user chat abuse controls, on top of the per-IP slowapi limit.

Two separate concerns:
  - **Volume**: an hourly message ceiling per user. A per-IP limit alone doesn't
    bound cost, since one account behind a NAT or a script can rotate addresses.
  - **Concurrency**: a cap on simultaneous open streams per user. Each stream
    pins a provider connection and a threadpool slot for its whole duration, so
    this is the control that protects the backend rather than the wallet.

Both **fail open** on Redis errors, matching slowapi's philosophy: a limiter
outage must degrade to "unlimited", never to "nobody can chat".
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_HOURLY_KEY = "rl:chat:msg:{user_id}:{hour}"
_ACTIVE_KEY = "chat:active:{user_id}"
_CANCEL_KEY = "chat:cancel:{message_uuid}"

# Leak guard: a stream whose process died without decrementing must not
# permanently consume a concurrency slot.
_ACTIVE_TTL_SECONDS = 900
_CANCEL_TTL_SECONDS = 600


def _redis():
    from app.core.redis import get_redis

    return get_redis()


def check_hourly_limit(user_id: int, limit: int) -> tuple[bool, int]:
    """Consume one unit of the user's hourly message budget.

    Args:
        user_id: The sender.
        limit: Messages allowed per rolling clock hour.

    Returns:
        ``(allowed, retry_after_seconds)``. ``retry_after`` is the time until the
        current hour window rolls over, for the ``Retry-After`` header.
    """
    now = int(time.time())
    hour = now // 3600
    key = _HOURLY_KEY.format(user_id=user_id, hour=hour)
    retry_after = 3600 - (now % 3600)

    try:
        client = _redis()
        count = client.incr(key)
        if count == 1:
            client.expire(key, 3600)
        if int(count) > limit:
            logger.info("Chat hourly limit hit for user %s (%s/%s)", user_id, count, limit)
            return False, retry_after
        return True, 0
    except Exception as exc:  # noqa: BLE001 — fail open
        logger.warning(f"Chat rate limiter unavailable (allowing): {exc}")
        return True, 0


def acquire_stream_slot(user_id: int, max_concurrent: int) -> bool:
    """Reserve one concurrent-stream slot; False when the user is at the cap."""
    key = _ACTIVE_KEY.format(user_id=user_id)
    try:
        client = _redis()
        active = client.incr(key)
        client.expire(key, _ACTIVE_TTL_SECONDS)
        if int(active) > max_concurrent:
            # Give the slot straight back — this attempt never held it.
            client.decr(key)
            logger.info(
                "Chat concurrency cap hit for user %s (%s active, max %s)",
                user_id,
                active - 1,
                max_concurrent,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — fail open
        logger.warning(f"Chat concurrency guard unavailable (allowing): {exc}")
        return True


def release_stream_slot(user_id: int) -> None:
    """Release a slot taken by :func:`acquire_stream_slot` (safe to over-call)."""
    key = _ACTIVE_KEY.format(user_id=user_id)
    try:
        client = _redis()
        if int(client.decr(key)) < 0:
            client.set(key, 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Could not release chat stream slot: {exc}")


def request_cancel(message_uuid: str) -> None:
    """Flag an in-flight generation for cancellation (explicit Stop button).

    Belt-and-braces beside client disconnect: a user on a flaky network may hit
    Stop over a fresh connection while the original request is still open.
    """
    try:
        _redis().setex(_CANCEL_KEY.format(message_uuid=message_uuid), _CANCEL_TTL_SECONDS, "1")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not set chat cancel flag: {exc}")


def is_cancelled(message_uuid: str) -> bool:
    """Whether a Stop was requested for this generation."""
    try:
        return bool(_redis().get(_CANCEL_KEY.format(message_uuid=message_uuid)))
    except Exception:  # noqa: BLE001
        return False


def clear_cancel(message_uuid: str) -> None:
    """Drop a cancel flag once the generation has finished."""
    try:
        _redis().delete(_CANCEL_KEY.format(message_uuid=message_uuid))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Could not clear chat cancel flag: {exc}")
