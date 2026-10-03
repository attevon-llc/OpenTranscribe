"""Find and recover transcription stages held by a worker that no longer exists.

Why this exists
---------------
The transcription stages are ``acks_late=True``: the broker keeps each message until the stage
finishes. On the Redis transport "keeps" means kombu's ``unacked`` hash (the message) plus its
``unacked_index`` sorted set (scored by delivery time). When a worker dies without a chance
to clean up (SIGKILL after a shutdown grace period, the kernel OOM killer, a lost node, a
container restart) nothing returns its messages:

* ``reject_on_worker_lost`` needs the prefork PARENT to survive and notice its child died;
  here the parent died too.
* kombu restores ``unacked`` messages at a clean channel close, which a killed process never
  reaches.
* kombu's ``restore_visible`` only restores a message once it is older than
  ``visibility_timeout`` -- six hours here, because Redis has no way to extend one message's
  visibility while its task is alive, so the timeout has to outlast the longest legitimate run.

So for up to six hours the stage was neither running nor queued: the file sat in PROCESSING,
``celery_queue_reserved`` counted the dead message as held work (keeping autoscaled capacity
up for nothing), and the eventual redelivery arrived as a surprise duplicate.

The application already knows which runs are alive: every executing transcription stage holds
a lease (``core/task_liveness.py``, refreshed every few seconds, ~90 s TTL). This module joins
the two views. An ``unacked`` stage message is ORPHANED when

1. it is a transcription stage (the only messages that carry a lease to check);
2. its run holds no live lease;
3. it was delivered more than ``BROKER_ORPHAN_STALE_SECONDS`` ago, and any ETA it carries has
   passed (a delivery that has not started yet has no lease to show); and, for the reaper only,
4. no live worker reports holding it (``inspect active/reserved/scheduled``), so a message
   buffered by a busy but healthy worker is never taken from it.

The reaper (``reclaim_orphaned_deliveries``, beat task ``system.reclaim_orphaned_deliveries``)
has two halves. When the message is still in ``unacked`` it puts each orphan back at the
FRONT of its own priority list using kombu's own
``QoS.restore_by_tag`` -- the same call kombu uses for a visibility-timeout restore -- so the
exact interrupted stage runs again, ahead of work submitted after it, within minutes. The
restore also removes the entry from ``unacked``, so it can never be redelivered six hours later.
Each restore spends one of the file's infrastructure requeues
(``services/transcription_retry.allow_infra_requeue``); past the cap the entry is discarded
and the file fails with a clear reason instead of cycling workers forever.

When the message is gone as well, there is nothing to put back: Celery's cold-shutdown cancel
ACKS a running ``acks_late`` task on the Redis transport (verified against celery 5.6 in
``tests/integration/test_orphaned_delivery_recovery_live.py``), and a broker that loses its
data loses the queue with it. The second half finds every in-flight transcription with no
lease and nothing queued (``services/transcription_retry.recover_lost_runs``) and dispatches
a replacement at retry priority, under the same infrastructure-requeue cap.

Duplicates stay harmless even if a judgement here is wrong: a stage re-checks ownership at
pickup (``tasks/transcription/run_ownership.py``) and stands down when its run was replaced or
finished, when another delivery of the same message still holds the lease, or when the
message already completed.

Every Redis or broker error fails safe: the sweep concludes nothing and changes nothing.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.task_config import task_recovery_config

logger = logging.getLogger(__name__)

#: The pipeline stages that hold a run lease while they execute. Only their messages can be
#: judged orphaned: every other ``acks_late`` task has no liveness signal to compare against.
TRANSCRIPTION_STAGES: frozenset[str] = frozenset(
    {
        "transcription.preprocess",
        "transcription.gpu_transcribe",
        "transcription.cpu_transcribe",
        "transcription.diarize_gpu",
        "transcription.postprocess",
    }
)


@dataclass(frozen=True)
class UnackedDelivery:
    """One entry of kombu's ``unacked`` hash, decoded as far as this module needs."""

    tag: str
    queue: str | None
    exchange: str | None
    task_name: str | None
    stage_id: str | None  # the Celery task id of the message
    run_id: str | None  # the application task id, for a transcription stage
    file_uuid: str | None
    delivered_at: float | None
    eta: float | None

    @property
    def is_stage(self) -> bool:
        return self.task_name in TRANSCRIPTION_STAGES and bool(self.run_id)

    def age(self, now: float) -> float | None:
        return None if self.delivered_at is None else now - self.delivered_at


def _decode(raw: bytes | str | None) -> str | None:
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else str(raw)


def _eta_epoch(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _run_identity(message: dict) -> tuple[str | None, str | None]:
    """``(run_id, file_uuid)`` from a stage message body, or ``(None, None)``.

    The first stage carries them as keyword arguments; every later stage receives the
    previous stage's payload dict positionally.
    """
    body = message.get("body")
    if body is None:
        return None, None
    try:
        if (message.get("properties") or {}).get("body_encoding") == "base64":
            body = base64.b64decode(body)
        decoded = json.loads(body)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(decoded, list) or len(decoded) < 2:
        return None, None
    args, kwargs = decoded[0] or [], decoded[1] or {}
    if isinstance(kwargs, dict) and kwargs.get("task_id"):
        return str(kwargs["task_id"]), _decode(kwargs.get("file_uuid"))
    for arg in args if isinstance(args, list) else []:
        if isinstance(arg, dict) and arg.get("task_id"):
            return str(arg["task_id"]), _decode(arg.get("file_uuid"))
    return None, None


def parse_unacked(tag: str, raw: bytes | str, delivered_at: float | None) -> UnackedDelivery:
    """Decode one ``unacked`` value (``[message, exchange, routing_key]``, kombu's format)."""
    message: dict = {}
    exchange = queue = None
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        decoded = None
    if isinstance(decoded, list) and len(decoded) == 3:
        message, exchange, queue = decoded
    headers = (message or {}).get("headers") or {}
    task_name = headers.get("task")
    run_id = file_uuid = None
    if task_name in TRANSCRIPTION_STAGES:
        run_id, file_uuid = _run_identity(message)
    return UnackedDelivery(
        tag=tag,
        queue=queue,
        exchange=exchange,
        task_name=task_name,
        stage_id=headers.get("id"),
        run_id=run_id,
        file_uuid=file_uuid,
        delivered_at=delivered_at,
        eta=_eta_epoch(headers.get("eta")),
    )


def read_unacked(client: Any) -> list[UnackedDelivery]:
    """Every ``unacked`` entry with its delivery time, in one round trip."""
    from kombu.transport.redis import Channel

    pipe = client.pipeline(transaction=False)
    pipe.hgetall(Channel.unacked_key)
    pipe.zrange(Channel.unacked_index_key, 0, -1, withscores=True)
    entries, scored = pipe.execute()
    delivered = {_decode(tag): float(score) for tag, score in scored or []}
    result = []
    for raw_tag, raw in (entries or {}).items():
        tag = _decode(raw_tag) or ""
        result.append(parse_unacked(tag, raw, delivered.get(tag)))
    return result


def _lease_states(run_ids: Iterable[str]) -> dict[str, Any]:
    from app.core.task_liveness import probe_runs

    return probe_runs(sorted(set(run_ids)))


def is_orphaned(delivery: UnackedDelivery, run: Any, now: float, stale: float) -> bool:
    """Criteria 1-3 of the module docstring (the cheap, broker-local ones)."""
    from app.core.task_liveness import RunState

    if not delivery.is_stage or run is None:
        return False
    if run.state in (RunState.RUNNING, RunState.UNKNOWN):
        return False
    age = delivery.age(now)
    if age is None or age < stale:
        return False
    return not (delivery.eta is not None and delivery.eta > now - stale)


def classify(
    deliveries: list[UnackedDelivery], now: float | None = None
) -> tuple[list[UnackedDelivery], list[UnackedDelivery]]:
    """Split ``deliveries`` into ``(held, orphaned)`` by the run leases.

    An unreadable lease store classifies nothing as orphaned.
    """
    now = time.time() if now is None else now
    stale = task_recovery_config.BROKER_ORPHAN_STALE
    runs = _lease_states(d.run_id for d in deliveries if d.is_stage and d.run_id)
    held: list[UnackedDelivery] = []
    orphaned: list[UnackedDelivery] = []
    for delivery in deliveries:
        run = runs.get(delivery.run_id) if delivery.run_id else None
        (orphaned if is_orphaned(delivery, run, now, stale) else held).append(delivery)
    return held, orphaned


# --- broker operations ------------------------------------------------------------------------


def _broker_connection():
    """A connection to the broker that gives up quickly instead of retrying forever.

    The app's own retry policy (``broker_connection_max_retries=None``) is right for a worker
    that must outlive a broker outage, and wrong here: the sweep and the recovery paths must
    fail fast and conclude nothing.
    """
    from app.core.celery import celery_app

    conn = celery_app.connection_for_write()
    try:
        conn.ensure_connection(max_retries=1)
    except Exception:
        conn.release()
        raise
    return conn


def restore_to_front(tag: str, connection: Any = None) -> bool:
    """Put one ``unacked`` message back at the head of its queue; False if it was not there.

    Uses kombu's ``QoS.restore_by_tag(leftmost=False)``: the same transaction kombu runs for a
    visibility-timeout restore, which removes the entry from ``unacked``/``unacked_index`` and
    RPUSHes the message onto its priority list, so it is the next one consumed at its priority.
    Refuses (False) when the message's exchange has no queue bindings, where kombu would
    otherwise divert it to an undeliverable-messages queue.
    """
    owns_connection = connection is None
    conn = _broker_connection() if owns_connection else connection
    try:
        channel = conn.default_channel
        client = channel.client
        raw = client.hget(channel.unacked_key, tag)
        if raw is None:
            return False
        _message, exchange, _routing_key = json.loads(raw)
        if exchange and not channel.get_table(exchange):
            logger.error(
                "Not restoring delivery %s: exchange %r has no queue bindings", tag, exchange
            )
            return False
        channel.qos.restore_by_tag(tag, leftmost=False)
        return not client.hexists(channel.unacked_key, tag)
    finally:
        if owns_connection:
            conn.release()


def discard_delivery(client: Any, tag: str) -> None:
    """Remove one entry from ``unacked``/``unacked_index`` without requeueing it.

    The same two commands kombu's own ack runs (``QoS._remove_from_indices``).
    """
    from kombu.transport.redis import Channel

    pipe = client.pipeline()
    pipe.zrem(Channel.unacked_index_key, tag)
    pipe.hdel(Channel.unacked_key, tag)
    pipe.execute()


def discard_run_deliveries(run_id: str, client: Any = None) -> int:
    """Drop every ``unacked`` message of run ``run_id``. Returns how many; 0 on any error.

    For a run that recovery has retired: its message would otherwise inflate the reserved
    metric and be redelivered when the visibility timeout ends.
    """
    conn = None
    try:
        if client is None:
            conn = _broker_connection()
            client = conn.default_channel.client
        dropped = 0
        for delivery in read_unacked(client):
            if delivery.run_id == run_id:
                discard_delivery(client, delivery.tag)
                dropped += 1
        return dropped
    except Exception as e:  # noqa: BLE001 - best effort; the stage's ownership check still guards
        logger.warning("Could not discard the broker deliveries of run %s: %s", run_id, e)
        return 0
    finally:
        if conn is not None:
            conn.release()


def requeue_own_delivery_to_front(stage_id: str, connection: Any = None) -> bool:
    """Requeue the message of the stage executing now (Celery id ``stage_id``) at the front.

    For a stage that stands down on purpose (graceful shutdown, a broken CUDA context): a plain
    ``Reject(requeue=True)`` makes kombu LPUSH the message to the BACK of its queue, behind every
    submission that arrived while it ran. The caller then raises ``Reject(requeue=False)``,
    whose kombu reject is a no-op on the entry this already moved. False (and nothing changed)
    when the message cannot be found, so the caller falls back to the plain requeue.
    """
    owns_connection = connection is None
    conn = _broker_connection() if owns_connection else connection
    try:
        client = conn.default_channel.client
        for delivery in read_unacked(client):
            if delivery.stage_id == stage_id:
                if delivery.run_id:
                    # The caller's Reject(requeue=False) makes the lease read "nothing queued";
                    # this message is queued, so say so first.
                    from app.core.task_liveness import mark_queued

                    mark_queued(delivery.run_id)
                return restore_to_front(delivery.tag, connection=conn)
        return False
    except Exception as e:  # noqa: BLE001 - the caller falls back to Reject(requeue=True)
        logger.warning("Could not requeue stage %s at the front: %s", stage_id, e)
        return False
    finally:
        if owns_connection:
            conn.release()


def held_by_live_workers(timeout: float = 3.0) -> set[str] | None:
    """Celery ids every live worker reports holding (running, buffered, or ETA-scheduled).

    None when the broadcast itself failed: the reaper must then conclude nothing.
    """
    try:
        from app.core.celery import celery_app

        inspect = celery_app.control.inspect(timeout=timeout)
        replies = [inspect.active(), inspect.reserved(), inspect.scheduled()]
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not ask the workers what they hold: %s", e)
        return None
    held: set[str] = set()
    for reply in replies:
        for requests in (reply or {}).values():
            for request in requests or []:
                inner = request.get("request", request) if isinstance(request, dict) else {}
                if inner.get("id"):
                    held.add(str(inner["id"]))
    return held


# --- the sweep --------------------------------------------------------------------------------


def _requeue_or_fail(delivery: UnackedDelivery, client: Any, connection: Any) -> str:
    """Act on one orphan. Returns the summary key for what happened."""
    from app.core.task_liveness import mark_queued
    from app.services import transcription_retry

    run_id = delivery.run_id or ""
    status = transcription_retry.run_status(run_id)
    if status == "unknown":
        return "skipped"
    if status == "finished":
        # A late copy of a run that already ended (or was replaced): never redeliver it.
        discard_delivery(client, delivery.tag)
        return "discarded"
    allowed, count = transcription_retry.allow_infra_requeue(delivery.file_uuid or run_id)
    if not allowed:
        discard_delivery(client, delivery.tag)
        transcription_retry.fail_interrupted_run(run_id)
        return "exhausted"
    # Queued BEFORE the restore, so no window exists in which the health check reads the run
    # as neither running nor queued and re-dispatches it beside the restored message.
    mark_queued(run_id)
    if not restore_to_front(delivery.tag, connection=connection):
        return "skipped"
    logger.warning(
        "Requeued %s of run %s (file %s) at the front of %s: its worker is gone "
        "(infrastructure requeue %d of %d)",
        delivery.task_name,
        run_id,
        delivery.file_uuid,
        delivery.queue,
        count,
        task_recovery_config.TRANSCRIPTION_MAX_INFRA_REQUEUES,
    )
    return "requeued"


def reclaim_orphaned_deliveries(
    connection: Any = None,
    held_ids: set[str] | None = None,
    now: float | None = None,
) -> dict[str, int]:
    """Requeue (or, past the cap, fail) every orphaned transcription stage. Counts by outcome.

    Args:
        connection: A kombu connection to the broker; defaults to the app's.
        held_ids: Celery ids live workers hold; defaults to asking them
            (:func:`held_by_live_workers`). Injectable for tests.
        now: Clock override for tests.
    """
    summary = {
        "unacked": 0,
        "orphaned": 0,
        "requeued": 0,
        "discarded": 0,
        "exhausted": 0,
        "skipped": 0,
        "lost_runs": 0,
    }
    owns_connection = connection is None
    conn = _broker_connection() if owns_connection else connection
    try:
        client = conn.default_channel.client
        deliveries = read_unacked(client)
        summary["unacked"] = len(deliveries)
        _held, orphaned = classify(deliveries, now=now)
        if orphaned:
            live = held_by_live_workers() if held_ids is None else held_ids
            if live is None:
                summary["error"] = 1
                return summary
            for delivery in orphaned:
                if delivery.stage_id in live:
                    continue
                summary["orphaned"] += 1
                summary[_requeue_or_fail(delivery, client, conn)] += 1
        # Runs whose worker AND message are gone: nothing to put back, so re-dispatch.
        from app.services.transcription_retry import recover_lost_runs

        in_broker = {d.run_id for d in deliveries if d.run_id}
        summary["lost_runs"] = recover_lost_runs(exclude=in_broker)
        return summary
    except Exception as e:  # noqa: BLE001 - conclude nothing from an unreadable broker
        logger.warning("Orphaned-delivery sweep skipped: %s", e)
        summary["error"] = 1
        return summary
    finally:
        if owns_connection:
            conn.release()
