"""Cross-process publication of which watch mode a source actually got.

The observer lives in the **celery-beat** process; the API that renders the
watch-sources panel lives in the **backend** process. Redis is the only thing
both already share, so the supervisor writes a small JSON blob per source and
the API reads it.

The key carries a TTL that the supervisor refreshes on every reconcile pass, so
a beat container that dies (or a fallback to polling) expires on its own instead
of leaving the UI claiming a watcher that no longer exists. Every operation is
best-effort: a Redis blip must never break an API response or the observer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

KEY_PREFIX = "watch_source:fs_status:"

# Comfortably longer than the supervisor's reconcile interval so a slow pass
# does not blink the badge, short enough that a dead beat disappears quickly.
DEFAULT_TTL_SECONDS = 120

# Observer modes reported to the UI.
MODE_NATIVE = "native"  # platform observer (inotify), delivery verified
MODE_POLLING = "polling"  # watchdog PollingObserver (stat sweep)
MODE_ERROR = "error"  # observer could not be started — Celery polling only
MODE_UNAVAILABLE = "unavailable"  # watchdog not installed in this image


def _key(source_id: int) -> str:
    return f"{KEY_PREFIX}{int(source_id)}"


def _client() -> Any | None:
    try:
        from app.core.redis import get_redis

        return get_redis()
    except Exception as e:  # noqa: BLE001 - status is best-effort
        logger.debug("FS-event status store unavailable: %s", e)
        return None


def publish(source_id: int, payload: dict[str, Any], ttl: int = DEFAULT_TTL_SECONDS) -> bool:
    """Write (and refresh the TTL of) one source's observer status."""
    client = _client()
    if client is None:
        return False
    try:
        client.setex(_key(source_id), ttl, json.dumps(payload, default=str))
        return True
    except Exception as e:  # noqa: BLE001 - status is best-effort
        logger.debug("Could not publish FS-event status for source %s: %s", source_id, e)
        return False


def clear(source_id: int) -> bool:
    """Drop a source's status (it is no longer watched)."""
    client = _client()
    if client is None:
        return False
    try:
        client.delete(_key(source_id))
        return True
    except Exception as e:  # noqa: BLE001 - status is best-effort
        logger.debug("Could not clear FS-event status for source %s: %s", source_id, e)
        return False


def get_many(source_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Fetch statuses for several sources in one round-trip.

    Missing / expired keys are simply absent from the result, which the API
    renders as "polling only".
    """
    if not source_ids:
        return {}
    client = _client()
    if client is None:
        return {}
    try:
        raw = client.mget([_key(sid) for sid in source_ids])
    except Exception as e:  # noqa: BLE001 - status is best-effort
        logger.debug("Could not read FS-event statuses: %s", e)
        return {}

    out: dict[int, dict[str, Any]] = {}
    for source_id, value in zip(source_ids, raw, strict=False):
        if not value:
            continue
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            out[int(source_id)] = parsed
    return out


def get(source_id: int) -> dict[str, Any] | None:
    """Fetch one source's status, or None when it is not being watched."""
    return get_many([source_id]).get(int(source_id))
