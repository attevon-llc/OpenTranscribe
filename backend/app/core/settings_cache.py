"""In-process TTL cache for system settings (read-side, single-key).

``services.system_settings_service.get_setting`` hits Postgres on every call.
The same handful of admin-tuned keys (engine config, redaction policy, retry
limits, …) are read many times per request and per Celery task. This module
caches single-key string reads in-process with a short TTL so hot reads avoid
the round-trip, while writes bust the affected key so callers never see a value
older than their own write.

Design / semantics
------------------
* ``cachetools.TTLCache`` (not thread-safe) guarded by a ``threading.Lock`` —
  the threadpooled sync FastAPI handlers and the Celery thread-pool workers
  share one process, so concurrent ``cached_get`` / ``invalidate`` must lock.
* TTL comes from ``settings.SETTINGS_CACHE_TTL`` (default 30s). ``TTL == 0``
  disables the cache entirely (every call is a straight DB read), which is the
  documented escape hatch.
* **Cross-process staleness is accepted by design.** Each process (the uvicorn
  API process and every Celery worker) keeps its own cache. A setting written
  in the API process busts only that process's cache; other processes see the
  old value for at most ``SETTINGS_CACHE_TTL`` seconds. Admin changes therefore
  propagate instantly in-process and ``<= TTL`` (30s) to workers. Only global
  admin settings live here — never per-user data — so there is no tenant-safety
  concern.

Test isolation (critical)
--------------------------
The unit suite runs inside savepoint-rolled-back transactions. A value cached in
one test would outlive its rollback and leak into the next test (and would also
defeat the savepoint suite's whole point — immediate setting reads must reflect
the setting just written in the same transaction). The cache therefore **fully
bypasses** when ``TESTING`` is truthy in the environment (``conftest`` sets
``TESTING=True`` at import time). The bypass is read at call time from
``os.environ`` so it is airtight regardless of import order — no test fixture is
required to keep the cache from leaking.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from cachetools import TTLCache

from app.core.config import settings
from app.core.metrics import cache_operations_total

_CACHE_NAME = "settings"

_lock = threading.Lock()
# maxsize is generous — the settings table has far fewer than 512 keys.
_cache: TTLCache[str, Any] = TTLCache(maxsize=512, ttl=max(settings.SETTINGS_CACHE_TTL, 1))


def _bypass() -> bool:
    """True when the cache must not be consulted (tests or TTL disabled)."""
    if settings.SETTINGS_CACHE_TTL <= 0:
        return True
    # Read at call time so import ordering can never defeat the test guard.
    return os.environ.get("TESTING", "").lower() == "true"


def cached_get(db: Any, key: str, default: Any = None) -> Any:
    """Return a system setting, served from the in-process TTL cache.

    On a miss the value is fetched from the database via the service layer and
    cached. Records ``cache_operations_total{cache="settings", result=...}``.

    Args:
        db: Database session (used only on a cache miss).
        key: Setting key.
        default: Value to return when the key is absent from the table.

    Returns:
        The cached/fetched string value, or ``default`` when not present.
    """
    # Import locally to avoid an import cycle (the service imports this module).
    from app.services.system_settings_service import _db_get_setting

    if _bypass():
        return _db_get_setting(db, key, default)

    with _lock:
        if key in _cache:
            cache_operations_total.labels(cache=_CACHE_NAME, result="hit").inc()
            value = _cache[key]
            return value if value is not None else default

    cache_operations_total.labels(cache=_CACHE_NAME, result="miss").inc()

    value = _db_get_setting(db, key, None)

    with _lock:
        _cache[key] = value
    return value if value is not None else default


def invalidate(key: str) -> None:
    """Drop a single key so the next read re-fetches from the database."""
    if _bypass():
        return
    with _lock:
        _cache.pop(key, None)
