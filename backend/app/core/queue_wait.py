"""How long a Celery message waited on the broker before a worker started it (issue #1172).

Every publish is stamped with the wall-clock header :data:`PUBLISHED_AT_HEADER` (from the
``before_task_publish`` handler in ``app/core/celery.py``). Two readers turn it into metrics:

* the worker, at ``task_prerun`` -> ``celery_task_queue_wait_seconds{queue, task}``
  (``app/core/worker_metrics.py``), for work that has started;
* the API's queue scrape, by peeking the oldest message of each broker list ->
  ``celery_queue_oldest_message_age_seconds{queue}`` (``app/core/celery_metrics.py``), for
  work still waiting.

What one stamp measures:

* **One hop.** A retry is a new message and is stamped afresh, so its wait is its own, not
  the first attempt's. Celery's ``Task.retry`` copies the previous message's custom headers
  onto the new one, so a stamp is only kept when it was made for the same message
  (:data:`PUBLISHED_FOR_HEADER` = ``"<id>:<retries>"``).
* **Redelivery keeps the original stamp.** A message the broker puts back after a lost worker
  or an expired visibility timeout is the same message, never re-published, so its wait
  includes the failed attempt. That is accurate for "how long has this work been waiting",
  but it is not pure queueing time.
* **Due time, not publish time, for a countdown/ETA.** A message with an ETA is measured from
  ``max(published_at, eta)`` — a deliberate delay is not queueing.
* **Wall clocks on two hosts.** Producer and worker clocks are compared directly; skew between
  them shifts every observation, and a producer clock ahead of the worker's is clamped to 0.
"""

from __future__ import annotations

import math
import time
from collections.abc import MutableMapping
from datetime import datetime
from typing import Any

from app.core.constants import CeleryQueues

PUBLISHED_AT_HEADER = "x-ot-published-at"

#: Which message the stamp was made for, so an inherited stamp is replaced on a retry.
PUBLISHED_FOR_HEADER = "x-ot-published-for"

#: The single ``queue`` label every unknown routing key is folded into.
OTHER_QUEUE = "other"

_KNOWN_QUEUES = frozenset(CeleryQueues.ALL)


def _message_identity(headers: MutableMapping[str, Any]) -> str:
    return f"{headers.get('id')}:{headers.get('retries') or 0}"


def stamp_published_at(headers: MutableMapping[str, Any] | None) -> None:
    """Stamp an outgoing message's headers with the publish time (``before_task_publish``)."""
    if headers is None:
        return
    identity = _message_identity(headers)
    if (
        headers.get(PUBLISHED_AT_HEADER) is not None
        and headers.get(PUBLISHED_FOR_HEADER) == identity
    ):
        return
    headers[PUBLISHED_AT_HEADER] = time.time()
    headers[PUBLISHED_FOR_HEADER] = identity


def parse_published_at(value: Any) -> float | None:
    """The stamp as an epoch float, or None when absent or unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    return stamp if math.isfinite(stamp) else None


def eta_epoch(value: Any) -> float | None:
    """A Celery ``eta`` (ISO-8601 text or datetime) as an epoch float, or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def waited_seconds(published_at: float, now: float, eta: Any = None) -> float:
    """Seconds from when the message became runnable until ``now``, never negative."""
    due = eta_epoch(eta)
    start = published_at if due is None else max(published_at, due)
    return max(0.0, now - start)


def queue_label(routing_key: Any) -> str:
    """A declared queue name, or :data:`OTHER_QUEUE`. Bounded by ``CeleryQueues.ALL``."""
    if isinstance(routing_key, str) and routing_key in _KNOWN_QUEUES:
        return routing_key
    return OTHER_QUEUE
