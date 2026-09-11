"""Celery queue-depth/reserved gauges, sampled at Prometheus scrape time.

``queue_snapshot`` is the single source of truth for "how much work is sitting
on a Celery queue", read directly off the Redis broker (db 0, the same
singleton the app uses everywhere else) in ONE round trip. Both
``update_queue_depths`` (the ``/metrics`` gauges) and
``app.utils.stats_helpers.get_queue_depths`` (the admin Statistics API) call
it — issue #892 found two separate implementations of this measurement that
disagreed, not that nobody knew how to measure it.

Priority-queue trap (verified against the installed kombu 5.6.2 and the live
``opentranscribe-redis`` container): this app sets
``broker_transport_options={"priority_steps": list(range(10)), ...}``
(``app/core/celery.py``), so kombu's Redis transport shards each queue into
per-priority lists named ``f"{queue}{sep}{priority}"`` with
``sep = kombu.transport.redis.Channel.sep`` (``'\\x06\\x16'``); priority 0 is
the bare queue name. A bare ``LLEN <queue>`` therefore UNDERCOUNTS — pending
depth is the sum across all 10 priority sub-lists. ``sep`` and the unacked
hash key are read off ``Channel`` at call time rather than hardcoded, so a
kombu upgrade that changes either is a version bump, not a silent miscount.

``reserved`` (issue #892's inversion fix): messages Redis has delivered to a
worker and not yet acknowledged, read from kombu's ``unacked`` hash in the
SAME round trip. This is *not* an "active tasks" count:

- included: every task still sitting in a worker's prefetch buffer, PLUS any
  task declared ``acks_late=True`` that is currently RUNNING (the whole
  transcription pipeline: ``tasks/transcription/postprocess.py``,
  ``diarize_task.py``, ``tasks/recovery.py``, ``tasks/speaker_clustering.py``)
- excluded: a RUNNING task with the default ``acks_late=False`` — celery acks
  those the instant the worker pool accepts them, so they never appear here

So ``reserved`` answers "work a worker is holding", not "work in progress".
Autoscale on ``celery_queue_depth + celery_queue_reserved``: depth alone
trends to zero as the fleet saturates, which is the inversion #892 named.

The whole module degrades gracefully: any broker error leaves the gauges
untouched (matches repo patterns; tests run with ``SKIP_REDIS=True``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.constants import CeleryQueues
from app.core.metrics import celery_queue_depth
from app.core.metrics import celery_queue_reserved

logger = logging.getLogger(__name__)

_PRIORITY_STEPS = range(10)

#: Bound on how many `unacked` entries we will attribute to queues in one
#: scrape. This app's real ceiling is Σ(prefetch × concurrency) across every
#: worker ≈ 37 (see backend/tests/unit/test_celery_queue_depth.py's cap test) —
#: 10_000 is a wide safety margin over that, not a tuned value. Above it we
#: skip attribution rather than pay an unbounded per-entry json.loads on an
#: endpoint whose docstring is about not blocking.
_RESERVED_ATTRIBUTION_LIMIT = 10_000


def queue_snapshot(client: Any = None) -> dict[str, dict[str, int]]:
    """``{queue: {"pending": N, "reserved": N}}`` for every declared queue, in ONE round trip.

    Args:
        client: A redis client. Defaults to :func:`app.core.redis.get_redis` —
            the BROKER (db 0), never ``celery_app.backend.client`` (the result
            backend, which coincides with the broker only because both default
            to ``REDIS_URL``; a deployment pointing ``CELERY_RESULT_BACKEND``
            elsewhere would silently report 0 forever).

    Returns:
        ``pending`` is the sum of the queue's 10 priority sub-lists.
        ``reserved`` is how many ``unacked`` entries name that queue as their
        routing key — zero (not an error) if attribution was skipped because
        the unacked hash exceeded :data:`_RESERVED_ATTRIBUTION_LIMIT`, or if
        anything in this function raised.
    """
    result: dict[str, dict[str, int]] = {
        name: {"pending": 0, "reserved": 0} for name in CeleryQueues.ALL
    }
    try:
        from kombu.transport.redis import Channel

        from app.core.redis import get_redis

        sep = Channel.sep
        unacked_key = Channel.unacked_key
        redis_client = client if client is not None else get_redis()

        pipe = redis_client.pipeline(transaction=False)
        for name in CeleryQueues.ALL:
            for priority in _PRIORITY_STEPS:
                key = name if priority == 0 else f"{name}{sep}{priority}"
                pipe.llen(key)
        pipe.hvals(unacked_key)
        *llens, unacked_values = pipe.execute()

        i = 0
        for name in CeleryQueues.ALL:
            pending = 0
            for _ in _PRIORITY_STEPS:
                pending += llens[i]
                i += 1
            result[name]["pending"] = pending

        if len(unacked_values) > _RESERVED_ATTRIBUTION_LIMIT:
            logger.debug(
                "Skipping reserved-task attribution: %d unacked entries exceeds the %d cap",
                len(unacked_values),
                _RESERVED_ATTRIBUTION_LIMIT,
            )
            return result

        for raw in unacked_values:
            try:
                _message, _exchange, routing_key = json.loads(raw)
            except Exception as exc:  # noqa: BLE001 — one corrupt entry must not break the scrape
                logger.debug("Skipping malformed unacked entry: %s", exc)
                continue
            if routing_key in result:
                result[routing_key]["reserved"] += 1

        return result
    except Exception as exc:  # noqa: BLE001 — scrape must never fail on broker issues
        logger.debug("Queue snapshot skipped: %s", exc)
        return {name: {"pending": 0, "reserved": 0} for name in CeleryQueues.ALL}


def update_queue_depths() -> None:
    """Refresh ``celery_queue_depth``/``celery_queue_reserved`` for every declared queue.

    ``celery_queue_depth`` keeps its pre-#892 meaning (pending only) — dashboards
    and alerts depend on that. ``celery_queue_reserved`` is new: unacked-from-the-
    broker, with the ``acks_late`` caveat in this module's docstring. Autoscale on
    ``celery_queue_depth + celery_queue_reserved``, which is exactly what the admin
    Statistics UI now displays via ``app.utils.stats_helpers.get_queue_depths``.
    """
    snapshot = queue_snapshot()
    for name, counts in snapshot.items():
        celery_queue_depth.labels(queue=name).set(counts["pending"])
        celery_queue_reserved.labels(queue=name).set(counts["reserved"])
