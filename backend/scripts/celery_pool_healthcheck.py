#!/usr/bin/env python3
"""Celery prefork-pool healthcheck: catch a pool that is not running tasks.

``celery inspect ping`` is answered by the worker's MainProcess regardless of
whether its forked child pool is alive, so a wedged prefork pool still reports
"healthy" to Docker. That is what went undetected for 10h46m in production
(issue #631): ``celery.concurrency.asynpool`` looping ``Timed out waiting for UP
message from <ForkProcess(...)>`` -> ``SIGKILL`` -> refork, 69,231 times, with
zero tasks consumed, while ``inspect ping`` kept answering — because MainProcess
itself was never the thing that died.

``inspect stats`` is answered by that SAME MainProcess but reports pool state,
so it can see what ``ping`` cannot. Two distinct failures are checked, because
the first one alone does **not** cover the incident above:

1. **Dead pool** — ``pool.processes`` is empty. Every child is gone and no
   replacement was forked.

2. **Respawn storm** — ``pool.processes`` is *non-empty* but the pool is
   thrashing. ⚠️ This is the shape #631 actually had, and a non-empty check
   reports it **healthy**: ``billiard.pool.Pool._create_worker_process`` appends
   the child to ``self._pool`` **before** ``w.start()``, and only removes it once
   ``_join_exited_workers`` observes a set ``exitcode``. A pool forking and
   killing N children a second therefore always has pids to report — they are
   just never the same pids, and none of them ever runs a task.

   The signature is exactly that pair: between two probes the pid set is
   **completely replaced** *and* the worker's accepted-task counter
   (``stats["total"]``, ``celery.worker.state.total_count``) has **not moved**.
   Both halves are required. Legitimate churn under ``--max-tasks-per-child``
   replaces pids too — but it replaces them *because* tasks completed, so the
   counter advances. A storm consumes nothing, so it cannot.

   Detection therefore needs memory: one prior sample is persisted next to the
   run and compared on the following probe. The first probe after a restart, and
   any probe whose predecessor is older than :data:`SAMPLE_STALE_AFTER_S`, report
   healthy rather than judging on data they do not have. Docker's ``retries``
   then requires the verdict to repeat before the container flips.

**Safe to point at any worker, whatever its pool.** The pool type is read from
``pool.implementation`` in the reply rather than assumed: a ``--pool=threads``
worker runs its pool inside MainProcess and reports no ``processes`` list at all,
so for it a successful ``inspect stats`` reply *is* the liveness proof that
``inspect ping`` provided, and the child-process checks are skipped rather than
failing it spuriously. That self-detection is what lets the threads-pool services
use this script too — three of them (``GPU_WORKER_POOL``,
``REDACTION_WORKER_POOL``, ``GPU_SCALE_POOL``) are prefork-*capable* by env var,
and ``core/celery.py`` explicitly recommends flipping the first one, which under
the old wiring silently downgraded them to the blind check.

**Cheap by construction.** This runs every 30 s in every worker container, so the
request is sent in-process through a bare ``Celery`` client built from the broker
URL (``app.core.broker_connection``), the documented ``app.control.inspect()`` API.
It used to shell out to ``celery -A app.core.celery inspect stats``: a second
interpreter that loaded the click CLI and its plugins (flower, click-repl and
prompt_toolkit, an entry-point scan) plus the whole app and pydantic-settings, about
2.4 s of CPU per probe for a broker round trip of under 0.1 s. The request and the
reply are the same ones the CLI sent and printed with ``--json``.
"""

from __future__ import annotations

import contextlib
import json
import re
import signal
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from celery import Celery
from kombu.exceptions import OperationalError

from app.core.broker_connection import broker_url_from_env
from app.core.broker_connection import redis_ssl_options

# Reply window handed to `inspect()`. Celery's own default is 1.0s (`Inspect`'s
# `timeout=1.0`), which `inspect stats` — answered by the MainProcess event loop —
# routinely misses on a busy worker, producing a false unhealthy that has nothing to
# do with pool health. The wait ends as soon as the one named node replies (kombu
# limits a destination-addressed broadcast to `len(destination)` replies).
INSPECT_REPLY_TIMEOUT_S = 5.0

# Wall-clock bound on the broker exchange. Together with the imports before it
# (about 1s cold) it MUST stay below the `healthcheck.timeout` in docker-compose.yml
# (10s), or Docker SIGKILLs the probe first and the deadline branch below, the one
# that says WHY it gave up, can never run (a blackholed broker measured 8.75s end to
# end with this at 8). A bound is needed beyond the reply window: kombu publishes
# control messages with `retry=True`, so an unreachable broker is retried, not failed.
INSPECT_TIMEOUT_S = 7

# A prior sample older than this is not compared against — the probe interval is
# 30s, so this tolerates four missed probes before falling back to "cannot judge".
SAMPLE_STALE_AFTER_S = 150.0


def _sample_path(worker_name: str) -> Path:
    """Where the previous probe's sample for *worker_name* is kept."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", worker_name)
    return Path(tempfile.gettempdir()) / f"celery-pool-healthcheck.{safe}.json"


def _read_previous_sample(path: Path) -> dict | None:
    """Return the previous sample, or None when absent/unreadable/stale."""
    try:
        sample = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(sample, dict) or "at" not in sample:
        return None
    if time.time() - float(sample["at"]) > SAMPLE_STALE_AFTER_S:
        return None
    return sample


def _write_sample(path: Path, pids: list, accepted: int) -> None:
    """Persist this probe's sample.

    Best-effort by design: losing the storm check is an acceptable degradation on a
    read-only filesystem, but a probe that *raises* is itself a false unhealthy and
    would take a working container down.
    """
    with contextlib.suppress(OSError):
        path.write_text(json.dumps({"at": time.time(), "pids": pids, "accepted": accepted}))


def _accepted_task_count(stats: dict) -> int:
    """Total tasks this worker has accepted, across all task names.

    ``stats["total"]`` is ``celery.worker.state.total_count``, a Counter keyed by
    task name that is incremented in ``task_accepted``. It is monotonic for the
    life of the MainProcess and is **not** reset when a child is recycled, which
    is what makes it a usable progress signal across probes.
    """
    total = stats.get("total")
    if isinstance(total, dict):
        return sum(v for v in total.values() if isinstance(v, int))
    if isinstance(total, int):
        return total
    return 0


class ProbeDeadlineError(Exception):
    """The broker exchange outlived :data:`INSPECT_TIMEOUT_S`."""


@contextlib.contextmanager
def _deadline(seconds: float) -> Iterator[None]:
    """Raise :class:`ProbeDeadlineError` in this (main) thread after *seconds*.

    ``SIGALRM`` is used because the blocking work is inside kombu/redis-py socket calls
    with no overall timeout of their own; a handler that raises interrupts them
    (PEP 475 retries an interrupted call only when the handler returns).
    """

    def _expired(signum: int, frame: object) -> None:
        raise ProbeDeadlineError

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _probe_app() -> Celery:
    """A broker-only Celery client: no task modules, no ML preamble, no settings import.

    Only what the control exchange depends on is configured — the broker URL and its TLS
    options. ``core/celery.py`` overrides no ``control_*`` setting and keeps celery's
    default ``json`` accept list, so the defaults here address the same pidbox.
    """
    url = broker_url_from_env()
    app = Celery("celery-pool-healthcheck", broker=url, set_as_current=False)
    app.conf.broker_use_ssl = redis_ssl_options(url)
    return app


def _inspect_stats(worker_name: str) -> dict[str, Any] | None:
    """``inspect stats`` for one node: ``{node: stats}``, or None when nobody replied."""
    app = _probe_app()
    try:
        with _deadline(INSPECT_TIMEOUT_S):
            inspector = app.control.inspect(
                destination=[worker_name], timeout=INSPECT_REPLY_TIMEOUT_S
            )
            reply: dict[str, Any] | None = inspector.stats()
            return reply
    finally:
        app.close()


def check_pool_has_live_processes(worker_name: str) -> tuple[bool, str]:
    """Return ``(healthy, reason)`` for the named worker's prefork pool.

    Args:
        worker_name: Full Celery node name, e.g. ``cpu-processor@somehost``.
    """
    try:
        payload = _inspect_stats(worker_name)
    except ProbeDeadlineError:
        return False, f"inspect stats gave no answer within {INSPECT_TIMEOUT_S}s"
    except (OperationalError, OSError) as exc:
        # Broker unreachable / refused / TLS failure (kombu re-raises redis-py connection
        # errors as OperationalError). Anything unforeseen propagates and exits 1, which
        # Docker reads as unhealthy too, so no exception can turn into a healthy verdict.
        return False, f"inspect stats failed: {type(exc).__name__}: {exc}"

    if not payload:
        return False, "no worker replied to inspect stats"

    stats = next(iter(payload.values()))
    pool = stats.get("pool")
    if not isinstance(pool, dict) or "implementation" not in pool:
        # Fail CLOSED on a reply we cannot interpret. A future celery could rename
        # this section; reporting healthy on unread data is how the original blind
        # check behaved and is the thing being fixed.
        return False, f"inspect stats reported no interpretable pool section: pool={pool!r}"

    implementation = str(pool["implementation"])
    if "prefork" not in implementation:
        # A threads (or eventlet/gevent) pool lives inside MainProcess, which just
        # answered — the same proof `inspect ping` gave, and the only one available.
        # Asserting on an absent `processes` list here would fail a healthy worker.
        return True, f"non-prefork pool answered ({implementation})"

    processes = pool.get("processes")
    if not processes:
        return False, f"prefork pool has no live child processes: pool={pool!r}"

    return _classify_pool_progress(worker_name, processes, _accepted_task_count(stats))


def _classify_pool_progress(worker_name: str, processes: list, accepted: int) -> tuple[bool, str]:
    """Decide between a working pool and a respawn storm, using the previous probe.

    Args:
        worker_name: Full Celery node name, used to key the persisted sample.
        processes: ``stats["pool"]["processes"]`` — the child pids as reported now.
        accepted: Tasks this worker has accepted so far (monotonic).
    """
    path = _sample_path(worker_name)
    previous = _read_previous_sample(path)
    _write_sample(path, list(processes), accepted)

    live = f"{len(processes)} live child process(es)"
    if previous is None:
        return True, f"{live} (no comparable prior sample; storm check deferred)"

    carried_over = set(processes) & set(previous.get("pids") or [])
    if carried_over:
        return True, f"{live}, {len(carried_over)} carried over from the previous probe"

    if accepted > int(previous.get("accepted", 0)):
        return True, f"{live}, pool fully recycled while accepting tasks (total={accepted})"

    return False, (
        f"prefork respawn storm: pool fully replaced since the previous probe "
        f"(was {previous.get('pids')!r}, now {list(processes)!r}) with no task accepted "
        f"(total stuck at {accepted}) — children are being SIGKILLed before they signal UP"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: celery_pool_healthcheck.py <worker-name>", file=sys.stderr)
        return 2

    healthy, reason = check_pool_has_live_processes(argv[1])
    print(reason, file=sys.stderr if not healthy else sys.stdout)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
