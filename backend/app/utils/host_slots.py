"""Non-blocking per-host concurrency slots backed by ``flock``.

Celery's ``--concurrency`` bounds how many tasks a worker runs, not how many of one
memory-heavy kind run together. Every prefork child of a worker shares the container's
filesystem and memory limit, so a lock file per slot under the temp dir bounds a task type
per container without a broker round trip.

``flock`` locks belong to the open file description, and the kernel drops them when the
holder exits for any reason, SIGKILL included. So a child killed mid-task can't leak its
slot, which a Redis counter with a TTL can.
"""

from __future__ import annotations

import fcntl
import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)


def _slot_dir(name: str) -> Path:
    path = Path(tempfile.gettempdir()) / "opentranscribe-host-slots" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def host_slot(name: str, limit: int) -> Iterator[bool]:
    """Try to take one of ``limit`` slots named ``name`` on this host without waiting.

    Yields True while a slot is held, or False when all are taken — the caller decides
    whether to defer. ``limit <= 0`` means unbounded and always yields True. If the slot
    directory can't be used the slot is granted (and logged): a broken temp dir must not
    stop the work, only its throttling.
    """
    if limit <= 0:
        yield True
        return
    try:
        directory = _slot_dir(name)
    except OSError as exc:
        logger.warning("Host slot dir for %s unusable (%s); running unthrottled", name, exc)
        yield True
        return

    for index in range(limit):
        fd = os.open(directory / f"{index}.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return
    yield False
