#!/usr/bin/env python3
"""Celery prefork-pool healthcheck: prove a child process can actually execute.

``celery inspect ping`` is answered by the worker's MainProcess regardless of
whether its forked child pool is alive — so a completely wedged prefork pool
(every child dead, unable to run any task) still reports "healthy" to Docker.
That is exactly what went undetected for 10h46m in production:
``celery.concurrency.asynpool`` looping ``Timed out waiting for UP message
from <ForkProcess(...)>`` -> SIGKILL, repeating (69,231 kills, zero tasks
consumed from the queue the entire time) while ``inspect ping`` kept
answering — because MainProcess itself was never the thing that died.

``inspect stats`` is answered by that SAME MainProcess, but its payload
*reports* the live child pool state (``pool.processes``, one pid per living
worker process). A wedged pool reports an empty list rather than a healthy
one lying about it, because MainProcess can only report what it can actually
see, and a pool with no children is observable regardless of what the
MainProcess event loop itself is doing.

Cheaper than round-tripping a real task through the broker (the alternative
considered for this same fix) and, unlike ``ping``, actually observes the
component that failed rather than a sibling that happens to still be alive.

Only for **prefork**-pool worker services (cpu-processor, media-downloader,
cloud-asr, ai-nlp, search-indexer). The ``--pool=threads`` workers
(gpu-transcription, gpu-transcribe, gpu-diarize, redaction) run their pool in
the SAME process as MainProcess, so ``inspect ping`` already proves liveness
for them — this script would report a spurious empty ``pool.processes`` there
(the threaded pool implementation does not populate that field the same way)
and must not be pointed at one.
"""

from __future__ import annotations

import json
import subprocess
import sys

INSPECT_TIMEOUT_S = 15


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
                "stats",
                "-d",
                worker_name,
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=INSPECT_TIMEOUT_S,
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
    processes = stats.get("pool", {}).get("processes")
    if not processes:
        return False, f"prefork pool has no live child processes: pool={stats.get('pool')!r}"

    return True, f"{len(processes)} live child process(es)"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: celery_pool_healthcheck.py <worker-name>", file=sys.stderr)
        return 2

    healthy, reason = check_pool_has_live_processes(argv[1])
    print(reason, file=sys.stderr if not healthy else sys.stdout)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
