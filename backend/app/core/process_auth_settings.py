"""Process-wide auth settings for enforcement points that hold no request session.

``get_process_auth_settings()`` is the session-less sibling of
``core/auth_settings.get_auth_settings(db)``: ``auth/password_policy.py`` is reached from a
Pydantic validator and ``auth/lockout.py`` from module-level functions whose callers pass only
an identifier, so neither can be handed a session. The returned object IS a
:class:`DynamicAuthSettings` (same accessors, same DB > .env > coded-default rule); it differs
only in opening its own short-lived session per cache generation and in accepting up to
``AUTH_CONFIG_CACHE_SECONDS`` of staleness in *other* processes.

This module owns that process-level cache: its single writer
(:func:`publish_process_auth_setting`), the post-commit prime ``AuthConfigService.set_config``
calls, and the clear the test suite's autouse fixture calls.
"""

import logging
import math
import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from app.core.auth_settings import DynamicAuthSettings

logger = logging.getLogger(__name__)


#: How long a process-level read stays cached. Matches ``SETTINGS_CACHE_TTL``'s
#: default and accepts the same cross-process staleness: a write in the API
#: process is instant there (``AuthConfigService.set_config`` primes this cache)
#: and reaches a Celery worker within the TTL.
AUTH_CONFIG_CACHE_SECONDS = 30.0

_process_cache: dict[str, Any] = {}
_process_cache_expiry: float = 0.0
_process_cache_lock = threading.Lock()


def _process_cache_ttl() -> float:
    """Seconds a process-level cache generation lives.

    Infinite under ``TESTING``: the unit suite runs inside savepoint-rolled-back
    transactions, so the only writer is an explicit prime from the test's own
    session (see :func:`prime_process_auth_settings`). Letting a generation age
    out mid-test would silently discard that prime and the test would start
    reading ``.env`` again. Isolation between tests comes from the autouse
    ``_clear_process_auth_cache`` fixture instead.
    """
    import os

    if os.environ.get("TESTING", "").lower() == "true":
        return math.inf
    return AUTH_CONFIG_CACHE_SECONDS


class _ProcessAuthSettings(DynamicAuthSettings):
    """Layered auth settings for call sites that hold no request session.

    Same DB > .env > coded-default rule as :class:`DynamicAuthSettings`; the
    session is opened here, per cache generation, instead of being passed in.
    Every failure mode falls back to the ``.env`` value rather than raising: a
    database hiccup must not be able to turn a login into a 500.
    """

    def __init__(self) -> None:
        # The base-class per-instance cache is bypassed — the module-level one is
        # shared by every caller and is what ``set_config`` primes.
        super().__init__(db=None, enable_cache=False)

    def get(self, key: str, default: Any = None) -> Any:
        """Return the effective value of *key*, cached process-wide.

        Args:
            key: Configuration key (lowercase, e.g. ``password_min_length``).
            default: Returned when neither the database nor ``.env`` has a value.

        Returns:
            The effective configuration value.
        """
        cached = _cache_lookup(key)
        if cached is not None:
            return cached

        db_value = _read_effective_from_own_session(key)
        if db_value is not None:
            publish_process_auth_setting(key, db_value)
            return db_value

        return super().get(key, default)


def _cache_lookup(key: str) -> Any:
    """Read *key* from the current cache generation, expiring a stale one."""
    global _process_cache_expiry

    with _process_cache_lock:
        if time.monotonic() >= _process_cache_expiry:
            _process_cache.clear()
            _process_cache_expiry = time.monotonic() + _process_cache_ttl()
            return None
        return _process_cache.get(key)


def publish_process_auth_setting(key: str, value: Any) -> None:
    """Publish an already-resolved effective value to the whole process.

    The single writer for the process-level cache: the database read path, the
    post-commit prime, and any caller that has resolved the effective value some
    other way all land here, so there is one place that decides what "current
    generation" means.

    ``None`` removes the key rather than caching it, because ``None`` is how the
    resolver spells "no value here — fall through to ``.env``".

    Args:
        key: Configuration key (lowercase).
        value: The effective value, already type-converted, or ``None`` to drop it.
    """
    global _process_cache_expiry

    with _process_cache_lock:
        if time.monotonic() >= _process_cache_expiry:
            _process_cache.clear()
            _process_cache_expiry = time.monotonic() + _process_cache_ttl()
        if value is None:
            _process_cache.pop(key, None)
        else:
            _process_cache[key] = value


def _read_effective_from_own_session(key: str) -> Any:
    """Resolve *key* against the database using a short-lived session.

    Returns ``None`` — meaning "fall back to ``.env``" — under ``TESTING`` and on
    any database error. Under ``TESTING`` a fresh session sits outside the
    suite's savepoint and would never see the row the test just wrote, so the
    honest answer is "no database value here".
    """
    import os

    if os.environ.get("TESTING", "").lower() == "true":
        return None

    try:
        from app.db.base import SessionLocal
        from app.services.auth_config_service import AuthConfigService

        db = SessionLocal()
        try:
            return AuthConfigService.get_effective_config(db, key)
        finally:
            db.close()
    except Exception:
        logger.warning(
            "Could not resolve auth config '%s' from the database; using the .env value.",
            key,
            exc_info=True,
        )
        return None


_process_auth_settings: _ProcessAuthSettings | None = None


def get_process_auth_settings() -> _ProcessAuthSettings:
    """Get the process-wide auth settings used where no session is available.

    Returns:
        The shared :class:`_ProcessAuthSettings` singleton.
    """
    global _process_auth_settings
    if _process_auth_settings is None:
        _process_auth_settings = _ProcessAuthSettings()
    return _process_auth_settings


def prime_process_auth_settings(db: Session, key: str) -> None:
    """Publish a freshly written value into the process-wide cache.

    Called by :meth:`AuthConfigService.set_config` right after the commit, so the
    process that accepted the admin's change enforces it on the very next
    request rather than up to a TTL later. It is also what makes the change
    visible to savepoint-isolated tests, whose rows no other session can see.

    Args:
        db: The session that wrote (and committed) the value.
        key: Configuration key that changed.
    """
    try:
        from app.services.auth_config_service import AuthConfigService

        value = AuthConfigService.get_effective_config(db, key)
    except Exception:
        logger.warning("Could not prime auth config '%s'; dropping the cache.", key, exc_info=True)
        clear_process_auth_settings_cache()
        return

    publish_process_auth_setting(key, value)


def clear_process_auth_settings_cache() -> None:
    """Drop every process-level value so the next read re-resolves it."""
    global _process_cache_expiry

    with _process_cache_lock:
        _process_cache.clear()
        _process_cache_expiry = 0.0
