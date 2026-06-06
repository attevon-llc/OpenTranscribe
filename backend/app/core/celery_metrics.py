"""Celery queue-depth gauges, sampled at Prometheus scrape time.

``update_queue_depths`` reads pending-task counts from the Celery broker
(Redis db 0, the same singleton the app uses) and sets ``celery_queue_depth``
per queue.

Priority-queue trap (verified): this app sets
``broker_transport_options={"priority_steps": list(range(10)), ...}``, so
kombu's Redis transport shards each queue into per-priority lists named
``f"{queue}{sep}{priority}"`` with ``sep = kombu.transport.redis.Channel.sep``
(``'\\x06\\x16'``); priority 0 is the bare queue name. A bare ``LLEN <queue>``
therefore UNDERCOUNTS — the depth is the sum across all 10 priority sub-lists.

The whole function degrades gracefully: any broker error leaves the gauges
untouched (matches repo patterns; tests run with ``SKIP_REDIS=True``).
"""

import logging

from app.core.constants import CeleryQueues
from app.core.metrics import celery_queue_depth

logger = logging.getLogger(__name__)

_PRIORITY_STEPS = range(10)


def update_queue_depths() -> None:
    """Refresh ``celery_queue_depth`` for every declared queue (best-effort)."""
    try:
        from kombu.transport.redis import Channel

        from app.core.redis import get_redis

        sep = Channel.sep
        redis_client = get_redis()

        for name in CeleryQueues.ALL:
            depth = 0
            for priority in _PRIORITY_STEPS:
                key = name if priority == 0 else f"{name}{sep}{priority}"
                depth += redis_client.llen(key)
            celery_queue_depth.labels(queue=name).set(depth)
    except Exception as exc:  # noqa: BLE001 — scrape must never fail on broker issues
        logger.debug("Queue-depth sampling skipped: %s", exc)
