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

``reserved`` EXCLUDES orphans. A worker killed while holding an ``acks_late``
message leaves it in ``unacked`` until the visibility timeout (six hours); counted
as reserved it kept autoscaled capacity up for hours with nothing to run. A
transcription-stage entry whose run holds no lease (``app/core/broker_orphans.py``)
is counted in ``orphaned`` instead, and the reaper moves it back onto the queue —
into ``pending`` — within about one sweep. Every other ``acks_late`` task has no
lease to check and stays in ``reserved``, as before.

The whole module degrades gracefully: any broker error leaves the gauges
untouched (matches repo patterns; tests run with ``SKIP_REDIS=True``).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.core.constants import CeleryQueues
from app.core.metrics import celery_queue_depth
from app.core.metrics import celery_queue_oldest_unacked_age_seconds
from app.core.metrics import celery_queue_orphaned
from app.core.metrics import celery_queue_reserved
from app.core.metrics import transcription_files_infra_requeued
from app.core.metrics import transcription_runs_without_lease

logger = logging.getLogger(__name__)

_PRIORITY_STEPS = range(10)

#: Bound on how many `unacked` entries we will attribute to queues in one
#: scrape. This app's real ceiling is Σ(prefetch × concurrency) across every
#: worker ≈ 37 (see backend/tests/unit/test_celery_queue_depth.py's cap test) —
#: 10_000 is a wide safety margin over that, not a tuned value. Above it we
#: skip attribution rather than pay an unbounded per-entry json.loads on an
#: endpoint whose docstring is about not blocking.
_RESERVED_ATTRIBUTION_LIMIT = 10_000


def _empty_counts() -> dict[str, int]:
    return {"pending": 0, "reserved": 0, "orphaned": 0, "oldest_unacked_age": 0}


def queue_snapshot(client: Any = None) -> dict[str, dict[str, int]]:
    """Per-queue counts for every declared queue, in ONE round trip to the broker.

    Args:
        client: A redis client. Defaults to :func:`app.core.redis.get_redis` —
            the BROKER (db 0), never ``celery_app.backend.client`` (the result
            backend, which coincides with the broker only because both default
            to ``REDIS_URL``; a deployment pointing ``CELERY_RESULT_BACKEND``
            elsewhere would silently report 0 forever).

    Returns:
        ``pending`` is the sum of the queue's 10 priority sub-lists.
        ``reserved`` is how many ``unacked`` entries name that queue as their
        routing key and still have a live holder; ``orphaned`` is the rest — a
        transcription stage whose run holds no lease (``app/core/broker_orphans.py``),
        i.e. a message a dead worker took with it. ``oldest_unacked_age`` is the age in
        seconds of the queue's oldest ``unacked`` entry, orphaned or not. All three are
        zero (not an error) if attribution was skipped because the unacked hash exceeded
        :data:`_RESERVED_ATTRIBUTION_LIMIT`, and everything is zero if anything raised.
        The run leases are read in a second, separate round trip, and only when an
        ``unacked`` entry is a transcription stage.
    """
    result: dict[str, dict[str, int]] = {name: _empty_counts() for name in CeleryQueues.ALL}
    try:
        from kombu.transport.redis import Channel

        from app.core.broker_orphans import classify
        from app.core.broker_orphans import parse_unacked
        from app.core.redis import get_redis

        sep = Channel.sep
        redis_client = client if client is not None else get_redis()

        pipe = redis_client.pipeline(transaction=False)
        for name in CeleryQueues.ALL:
            for priority in _PRIORITY_STEPS:
                key = name if priority == 0 else f"{name}{sep}{priority}"
                pipe.llen(key)
        pipe.hgetall(Channel.unacked_key)
        pipe.zrange(Channel.unacked_index_key, 0, -1, withscores=True)
        *llens, unacked, scored = pipe.execute()

        i = 0
        for name in CeleryQueues.ALL:
            pending = 0
            for _ in _PRIORITY_STEPS:
                pending += llens[i]
                i += 1
            result[name]["pending"] = pending

        unacked = unacked or {}
        if len(unacked) > _RESERVED_ATTRIBUTION_LIMIT:
            logger.debug(
                "Skipping reserved-task attribution: %d unacked entries exceeds the %d cap",
                len(unacked),
                _RESERVED_ATTRIBUTION_LIMIT,
            )
            return result

        delivered = {_text(tag): float(score) for tag, score in scored or []}
        deliveries = []
        for raw_tag, raw in unacked.items():
            tag = _text(raw_tag)
            delivery = parse_unacked(tag, raw, delivered.get(tag))
            if delivery.queue is None:
                logger.debug("Skipping malformed unacked entry %s", tag)
                continue
            deliveries.append(delivery)

        now = time.time()
        held, orphaned = classify(deliveries, now=now)
        for bucket, deliveries_in in (("reserved", held), ("orphaned", orphaned)):
            for delivery in deliveries_in:
                counts = result.get(delivery.queue or "")
                if counts is None:
                    continue
                counts[bucket] += 1
                age = int(delivery.age(now) or 0)
                counts["oldest_unacked_age"] = max(counts["oldest_unacked_age"], age)

        return result
    except Exception as exc:  # noqa: BLE001 — scrape must never fail on broker issues
        logger.debug("Queue snapshot skipped: %s", exc)
        return {name: _empty_counts() for name in CeleryQueues.ALL}


def _text(raw: bytes | str) -> str:
    return raw.decode() if isinstance(raw, bytes) else str(raw)


def update_queue_depths() -> None:
    """Refresh the per-queue gauges for every declared queue.

    ``celery_queue_depth`` keeps its pre-#892 meaning (pending only) — dashboards
    and alerts depend on that. ``celery_queue_reserved`` is unacked-from-the-broker
    with a live holder, with the ``acks_late`` caveat in this module's docstring.
    Autoscale on ``celery_queue_depth + celery_queue_reserved``, which is exactly
    what the admin Statistics UI displays via ``app.utils.stats_helpers.get_queue_depths``.
    ``celery_queue_orphaned`` and ``celery_queue_oldest_unacked_age_seconds`` are for
    alerting, never for scaling: an orphan is work no worker holds, and the reaper puts
    it back on the queue (where it counts as depth) within about one sweep.
    """
    snapshot = queue_snapshot()
    for name, counts in snapshot.items():
        celery_queue_depth.labels(queue=name).set(counts["pending"])
        celery_queue_reserved.labels(queue=name).set(counts["reserved"])
        celery_queue_orphaned.labels(queue=name).set(counts["orphaned"])
        celery_queue_oldest_unacked_age_seconds.labels(queue=name).set(counts["oldest_unacked_age"])


def update_transcription_lease_metrics() -> None:
    """Refresh the two transcription-recovery alert gauges (full ``/metrics`` page only).

    ``transcription_runs_without_lease``: non-terminal transcription runs that are neither
    running nor queued -- what recovery exists to fix, so it should read 0.
    ``transcription_files_infra_requeued``: files at or past the poison-alert threshold.
    One indexed query and one MGET; any failure leaves the gauges untouched.
    """
    import os

    try:
        from app.core.task_liveness import TRANSCRIPTION_TASK_TYPE
        from app.core.task_liveness import RunState
        from app.core.task_liveness import count_files_requeued_at_least
        from app.core.task_liveness import probe_runs
        from app.db.session_utils import session_scope
        from app.models.media import Task

        with session_scope() as db:
            run_ids = [
                str(row[0])
                for row in db.query(Task.id).filter(
                    Task.task_type == TRANSCRIPTION_TASK_TYPE,
                    Task.status.in_(["pending", "in_progress"]),
                )
            ]
        runs = probe_runs(run_ids)
        if all(run.state != RunState.UNKNOWN for run in runs.values()):
            dead = sum(1 for run in runs.values() if run.state == RunState.DEAD)
            transcription_runs_without_lease.set(dead)
        threshold = max(1, int(os.getenv("TRANSCRIPTION_INFRA_REQUEUE_ALERT_THRESHOLD", "3")))
        requeued = count_files_requeued_at_least(threshold)
        if requeued is not None:
            transcription_files_infra_requeued.set(requeued)
    except Exception as exc:  # noqa: BLE001 — scrape must never fail on a backing store
        logger.debug("Transcription lease metrics skipped: %s", exc)
