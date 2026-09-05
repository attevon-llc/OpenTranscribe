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
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Reply window handed to `celery inspect`. Celery's own default is 1.0s
# (`celery/bin/control.py`), which `inspect stats` — answered by the MainProcess
# event loop — routinely misses on a busy worker, producing a false unhealthy
# that has nothing to do with pool health.
INSPECT_REPLY_TIMEOUT_S = 5.0

# Wall-clock bound on the whole `celery` invocation. MUST stay below the
# `healthcheck.timeout` in docker-compose.yml (10s) or Docker kills the probe
# first and the TimeoutExpired branch below can never run.
INSPECT_TIMEOUT_S = 8

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


def check_pool_has_live_processes(worker_name: str) -> tuple[bool, str]:
    """Return ``(healthy, reason)`` for the named worker's prefork pool.

    Args:
        worker_name: Full Celery node name, e.g. ``cpu-processor@somehost``.
    """
    try:
        proc = subprocess.run(  # noqa: S603  # nosec B603
            # Fixed argv, no shell; `worker_name` is a Docker healthcheck's own
            # `$$HOSTNAME`-derived node name, not external/user input. `celery`
            # is resolved via PATH deliberately — same convention as every other
            # subprocess call in this codebase that shells out to a tool
            # installed into the image's venv rather than an absolute path.
            [  # noqa: S607  # nosec B607
                "celery",
                "-A",
                "app.core.celery",
                "inspect",
                "--timeout",
                str(INSPECT_REPLY_TIMEOUT_S),
                "stats",
                "-d",
                worker_name,
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=INSPECT_TIMEOUT_S,
            # The probe is a CLI *client*: it broadcasts and reads a reply, it never
            # runs a task. `SKIP_CELERY` skips only `core/celery.py`'s ML preamble
            # (the torch import and its `torch.load` patch), leaving the app, broker
            # and queue config identical. Measured on a live worker: 7.0-7.7s -> 2.8s
            # per probe. That matters because the pre-existing 7.3s ran against a 10s
            # compose `healthcheck.timeout`, five containers deep, every 30s — close
            # enough that ordinary load could flap a healthy worker.
            env={**os.environ, "SKIP_CELERY": "true"},
        )
    except subprocess.TimeoutExpired:
        return False, "celery inspect stats timed out"

    if proc.returncode != 0 or not proc.stdout.strip():
        return False, f"celery inspect stats failed (rc={proc.returncode}): {proc.stderr.strip()}"

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return False, f"could not parse inspect stats output: {exc}"

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
