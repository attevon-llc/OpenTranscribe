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
import uuid

logger = logging.getLogger(__name__)

_HOURLY_KEY = "rl:chat:msg:{user_id}:{hour}"
_ACTIVE_KEY = "chat:active:{user_id}"
_CANCEL_KEY = "chat:cancel:{message_uuid}"

# Leak guard: a stream whose process died without releasing must not permanently
# consume a concurrency slot. Slots are tracked INDIVIDUALLY (a sorted set keyed
# by stream id, scored by start time) rather than as one counter with a TTL: a
# counter's expiry is refreshed on every acquire, so for an active user a leaked
# slot never ages out and their usable concurrency degrades 2 -> 1 -> 0 with no
# way back short of flushing the key by hand.
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


def _drop_legacy_counter(client, key: str) -> None:
    """Delete a pre-sorted-set counter left at ``key`` by an older version."""
    try:
        if client.type(key) == "string" or client.type(key) == b"string":
            client.delete(key)
            logger.info("Retired legacy chat concurrency counter at %s", key)
    except Exception as exc:  # noqa: BLE001 — never block a chat over cleanup
        logger.debug(f"Could not inspect chat slot key type: {exc}")


def acquire_stream_slot(
    user_id: int, max_concurrent: int, slot_id: str | None = None
) -> str | None:
    """Reserve one concurrent-stream slot.

    Args:
        user_id: The streaming user.
        max_concurrent: Cap from the admin settings.
        slot_id: Identifier for this stream; generated when omitted.

    Returns:
        The slot id to pass to :func:`release_stream_slot`, or ``None`` when the
        user is already at the cap.

    Stale slots are pruned by age on every acquire, so a stream that died
    without releasing frees its own slot after ``_ACTIVE_TTL_SECONDS`` and
    cannot accumulate against a user who keeps chatting.
    """
    key = _ACTIVE_KEY.format(user_id=user_id)
    slot = slot_id or uuid.uuid4().hex
    now = time.time()
    try:
        client = _redis()
        # An upgraded deployment still holds the previous implementation's
        # INTEGER counter at this key. Every sorted-set command against it
        # raises WRONGTYPE, which the fail-open handler below would swallow —
        # leaving the concurrency cap silently disabled until the stale key
        # happened to expire. Retire it once, on first contact.
        _drop_legacy_counter(client, key)
        # Drop slots older than the leak window before counting.
        client.zremrangebyscore(key, "-inf", now - _ACTIVE_TTL_SECONDS)
        if int(client.zcard(key)) >= max_concurrent:
            logger.info(
                "Chat concurrency cap hit for user %s (max %s)",
                user_id,
                max_concurrent,
            )
            return None
        client.zadd(key, {slot: now})
        # Whole-key expiry is only a backstop for a user who never returns; the
        # per-member prune above is what actually bounds a leak.
        client.expire(key, _ACTIVE_TTL_SECONDS * 2)
        return slot
    except Exception as exc:  # noqa: BLE001 — fail open
        logger.warning(f"Chat concurrency guard unavailable (allowing): {exc}")
        return slot


def release_stream_slot(user_id: int, slot_id: str | None = None) -> None:
    """Release a slot taken by :func:`acquire_stream_slot` (safe to over-call).

    Removing by id makes this idempotent: a double release cannot free someone
    else's in-flight stream, which a bare decrement could.
    """
    key = _ACTIVE_KEY.format(user_id=user_id)
    try:
        client = _redis()
        if slot_id:
            client.zrem(key, slot_id)
        else:
            # No id (older call site): prune by age only, never blanket-delete —
            # that would free slots still legitimately held.
            client.zremrangebyscore(key, "-inf", time.time() - _ACTIVE_TTL_SECONDS)
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
