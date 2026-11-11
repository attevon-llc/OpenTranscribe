"""Point every synchronous Redis accessor of ``app.core.redis`` at one client.

``app.core.redis`` hands out more than one db-0 client: ``get_redis()`` (unbounded, worker
paths), ``get_request_redis()`` (bounded, request paths) and ``get_probe_redis()`` (probes).
A test that redirects only ``get_redis`` to its own database silently leaves every caller of
the others on the real db 0. That is how the live broker suites broke: 1fe7bf595 moved
``task_replay``, ``task_liveness`` and ``celery_metrics`` onto ``get_request_redis()``, so the
lease reads and queue depths those suites assert on went to a database the test never wrote.

The accessor list is read from the module, not written down here, so an accessor added later
is routed too: any public ``get_*`` function whose return annotation is the synchronous
``redis.Redis``. The async accessor (``get_async_redis``) is a different class and is left alone.
"""

from __future__ import annotations

import inspect
import typing
from typing import Any

import pytest
import redis


def sync_redis_accessor_names() -> list[str]:
    """Names of the public ``app.core.redis`` functions that return a synchronous client."""
    from app.core import redis as redis_core

    names = []
    for name, fn in vars(redis_core).items():
        if not name.startswith("get_") or not callable(fn):
            continue
        target = inspect.unwrap(fn)
        if not inspect.isfunction(target) or target.__module__ != redis_core.__name__:
            continue
        if typing.get_type_hints(target).get("return") is redis.Redis:
            names.append(name)
    return sorted(names)


def route_redis_accessors(monkeypatch: pytest.MonkeyPatch, client: Any) -> list[str]:
    """Make every synchronous accessor return ``client`` for the rest of the test.

    Returns:
        The accessor names that were patched, so a caller can assert on the set.
    """
    names = sync_redis_accessor_names()
    for name in names:
        monkeypatch.setattr(f"app.core.redis.{name}", lambda: client)
    return names
