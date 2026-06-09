"""Global Watch-Source settings, DB-backed via ``SystemSettings``.

These are the handful of *global* tuning knobs (the per-source connection,
credential, and schedule settings live on the ``watch_source`` row). They are
stored in ``system_settings`` and edited from the admin UI, so they take effect
on the next scan with **no restart**. Coded ``DEFAULT_WATCH_*`` constants are the
fallback — there are no watch tuning ``.env`` vars.

Each getter accepts an optional ``db`` session; when omitted it opens a
short-lived one (handy from contexts like the local client that don't already
hold a session).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

from sqlalchemy.orm import Session

from app.core.constants import DEFAULT_WATCH_ENABLED
from app.core.constants import DEFAULT_WATCH_FILE_STABILITY_SECONDS
from app.core.constants import DEFAULT_WATCH_FS_EVENTS_ENABLED
from app.core.constants import DEFAULT_WATCH_MAX_CONCURRENT_IMPORTS
from app.services import system_settings_service

logger = logging.getLogger(__name__)

KEY_ENABLED = "watch.enabled"
KEY_FILE_STABILITY_SECONDS = "watch.file_stability_seconds"
KEY_MAX_CONCURRENT_IMPORTS = "watch.max_concurrent_imports"
KEY_FS_EVENTS_ENABLED = "watch.fs_events_enabled"


@contextlib.contextmanager
def _session(db: Session | None) -> Iterator[Session]:
    """Yield the passed session, or a short-lived one that is closed after use."""
    if db is not None:
        yield db
        return
    from app.db.base import SessionLocal

    own = SessionLocal()
    try:
        yield own
    finally:
        own.close()


def is_enabled(db: Session | None = None) -> bool:
    with _session(db) as s:
        return system_settings_service.get_setting_bool(s, KEY_ENABLED, DEFAULT_WATCH_ENABLED)


def file_stability_seconds(db: Session | None = None) -> int:
    with _session(db) as s:
        return system_settings_service.get_setting_int(
            s, KEY_FILE_STABILITY_SECONDS, DEFAULT_WATCH_FILE_STABILITY_SECONDS
        )


def max_concurrent_imports(db: Session | None = None) -> int:
    with _session(db) as s:
        return system_settings_service.get_setting_int(
            s, KEY_MAX_CONCURRENT_IMPORTS, DEFAULT_WATCH_MAX_CONCURRENT_IMPORTS
        )


def fs_events_enabled(db: Session | None = None) -> bool:
    with _session(db) as s:
        return system_settings_service.get_setting_bool(
            s, KEY_FS_EVENTS_ENABLED, DEFAULT_WATCH_FS_EVENTS_ENABLED
        )


def get_global_settings(db: Session | None = None) -> dict[str, Any]:
    """Return all global watch settings as a dict (single SELECT for all keys)."""
    with _session(db) as s:
        vals = system_settings_service.get_settings_map(
            s,
            [
                KEY_ENABLED,
                KEY_FILE_STABILITY_SECONDS,
                KEY_MAX_CONCURRENT_IMPORTS,
                KEY_FS_EVENTS_ENABLED,
            ],
        )

        def _b(key: str, default: bool) -> bool:
            v = vals.get(key)
            return v.lower() in ("true", "1", "yes", "on") if v is not None else default

        def _i(key: str, default: int) -> int:
            v = vals.get(key)
            if v is None:
                return default
            try:
                return int(v)
            except (ValueError, TypeError):
                return default

        return {
            "enabled": _b(KEY_ENABLED, DEFAULT_WATCH_ENABLED),
            "file_stability_seconds": _i(
                KEY_FILE_STABILITY_SECONDS, DEFAULT_WATCH_FILE_STABILITY_SECONDS
            ),
            "max_concurrent_imports": _i(
                KEY_MAX_CONCURRENT_IMPORTS, DEFAULT_WATCH_MAX_CONCURRENT_IMPORTS
            ),
            "fs_events_enabled": _b(KEY_FS_EVENTS_ENABLED, DEFAULT_WATCH_FS_EVENTS_ENABLED),
        }


def update_global_settings(
    db: Session,
    *,
    enabled: bool | None = None,
    file_stability_seconds: int | None = None,
    max_concurrent_imports: int | None = None,
    fs_events_enabled: bool | None = None,
) -> dict[str, Any]:
    """Persist any provided global watch settings; return the full current set."""
    if enabled is not None:
        system_settings_service.set_setting(db, KEY_ENABLED, enabled, "Watch Sources master toggle")
    if file_stability_seconds is not None:
        system_settings_service.set_setting(
            db,
            KEY_FILE_STABILITY_SECONDS,
            int(file_stability_seconds),
            "Skip files modified within N seconds (still being written)",
        )
    if max_concurrent_imports is not None:
        system_settings_service.set_setting(
            db,
            KEY_MAX_CONCURRENT_IMPORTS,
            int(max_concurrent_imports),
            "Max files imported concurrently per watch-source scan",
        )
    if fs_events_enabled is not None:
        system_settings_service.set_setting(
            db,
            KEY_FS_EVENTS_ENABLED,
            fs_events_enabled,
            "Enable the optional watchdog FS-event layer (polling stays the baseline)",
        )
    return get_global_settings(db)
