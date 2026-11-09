"""Prometheus multiprocess mode for the API when it runs more than one uvicorn worker.

With ``UVICORN_WORKERS`` > 1 each worker is its own process with its own copy of every
collector, and Prometheus scrapes whichever one the kernel hands the request to. The
documented fix is ``prometheus_client``'s multiprocess mode ("Multiprocess Mode",
https://prometheus.github.io/client_python/multiprocess/): every process writes its samples
to memory-mapped files in ``PROMETHEUS_MULTIPROC_DIR`` and the scrape aggregates all of them
with ``MultiProcessCollector`` on a fresh registry. ``scripts/api-start.sh`` sets and wipes
the directory before any worker starts, as the docs require; with it unset (dev's single
``--reload`` process, tests, one worker) everything uses the plain default registry.

What the docs say that matters here, and what this module does about it:

* Gauges need an explicit ``multiprocess_mode``. Per-process quantities (requests in flight,
  the database pool) are ``livesum``; values any process refreshes for the whole deployment at
  scrape time (queue depths, backup and outbox state) are ``mostrecent``, which supports only
  ``set``. ``tests/unit/test_gauge_multiprocess_modes.py`` fails on a Gauge without one.
* Counters and histograms keep a dead process's totals by design (rates stay correct).
* ``live*`` gauge files of a dead process must be removed with ``mark_process_dead(pid)``.
  gunicorn calls it from ``child_exit``; uvicorn's supervisor has no such hook, so a worker
  that was restarted would leave its last in-flight and pool values summed into the total
  forever. :func:`install` therefore sweeps files of pids that are no longer alive when a
  worker imports the metrics, and marks the process's own pid dead at clean exit.
* Limitations: the default registry's process/platform/GC collectors are per process and are
  not part of the aggregated page; custom collectors are not supported (the pool gauges are
  plain Gauges driven by pool events for that reason).
"""

from __future__ import annotations

import atexit
import logging
import os
import re
from pathlib import Path

from prometheus_client import REGISTRY
from prometheus_client import CollectorRegistry
from prometheus_client import generate_latest
from prometheus_client import multiprocess

from app.core.config import settings

logger = logging.getLogger(__name__)

#: ``<kind>_<pid>.db`` (``gauge_livesum_<pid>.db``, ``counter_<pid>.db``, ...).
_FILE_PID = re.compile(r"_(\d+)\.db$")

_installed = False


def enabled() -> bool:
    """Whether this process is in multiprocess mode (the directory is set and non-empty)."""
    return bool(settings.PROMETHEUS_MULTIPROC_DIR)


def render_all() -> bytes:
    """The Prometheus text page for the whole deployment (all workers) or this process."""
    if not enabled():
        return generate_latest(REGISTRY)
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry, path=settings.PROMETHEUS_MULTIPROC_DIR)
    return generate_latest(registry)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_dead_processes() -> list[int]:
    """Drop the ``live*`` gauge files of every pid in the directory that no longer exists."""
    if not enabled():
        return []
    swept: list[int] = []
    pids = {
        int(match.group(1))
        for path in Path(settings.PROMETHEUS_MULTIPROC_DIR).glob("*.db")
        if (match := _FILE_PID.search(path.name))
    }
    for pid in sorted(pids):
        if not _alive(pid):
            multiprocess.mark_process_dead(pid, path=settings.PROMETHEUS_MULTIPROC_DIR)
            swept.append(pid)
    return swept


def _mark_self_dead() -> None:
    try:
        multiprocess.mark_process_dead(os.getpid(), path=settings.PROMETHEUS_MULTIPROC_DIR)
    except OSError:
        logger.debug("could not remove this process's live gauge files", exc_info=True)


def install() -> None:
    """Once per process, in multiprocess mode: sweep dead workers, arrange our own cleanup."""
    global _installed
    if _installed or not enabled():
        return
    _installed = True
    swept = sweep_dead_processes()
    if swept:
        logger.info("Removed live gauge files of exited API workers: %s", swept)
    atexit.register(_mark_self_dead)
