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
from app.core.constants import DEFAULT_WATCH_MAX_IMPORTS_PER_SCAN
from app.services import system_settings_service

logger = logging.getLogger(__name__)

KEY_ENABLED = "watch.enabled"
KEY_FILE_STABILITY_SECONDS = "watch.file_stability_seconds"
KEY_MAX_IMPORTS_PER_SCAN = "watch.max_imports_per_scan"
KEY_FS_EVENTS_ENABLED = "watch.fs_events_enabled"

# Pre-#295 name for KEY_MAX_IMPORTS_PER_SCAN. Read-only fallback so a deployment that
# configured the old key keeps its value across the upgrade without a data migration;
# writes only ever go to the new key, so the legacy row goes inert on the next save.
LEGACY_KEY_MAX_CONCURRENT_IMPORTS = "watch.max_concurrent_imports"


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


def max_imports_per_scan(db: Session | None = None) -> int:
    """Max standalone files one scan imports (a per-scan cap, not a concurrency limit)."""
    with _session(db) as s:
        legacy_default = system_settings_service.get_setting_int(
            s, LEGACY_KEY_MAX_CONCURRENT_IMPORTS, DEFAULT_WATCH_MAX_IMPORTS_PER_SCAN
        )
        return system_settings_service.get_setting_int(s, KEY_MAX_IMPORTS_PER_SCAN, legacy_default)


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
                KEY_MAX_IMPORTS_PER_SCAN,
                LEGACY_KEY_MAX_CONCURRENT_IMPORTS,
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
            "max_imports_per_scan": _i(
                KEY_MAX_IMPORTS_PER_SCAN,
                _i(LEGACY_KEY_MAX_CONCURRENT_IMPORTS, DEFAULT_WATCH_MAX_IMPORTS_PER_SCAN),
            ),
            "fs_events_enabled": _b(KEY_FS_EVENTS_ENABLED, DEFAULT_WATCH_FS_EVENTS_ENABLED),
        }


def update_global_settings(
    db: Session,
    *,
    enabled: bool | None = None,
    file_stability_seconds: int | None = None,
    max_imports_per_scan: int | None = None,
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
    if max_imports_per_scan is not None:
        system_settings_service.set_setting(
            db,
            KEY_MAX_IMPORTS_PER_SCAN,
            int(max_imports_per_scan),
            "Max files imported per watch-source scan (serial, not concurrent)",
        )
    if fs_events_enabled is not None:
        system_settings_service.set_setting(
            db,
            KEY_FS_EVENTS_ENABLED,
            fs_events_enabled,
            "Enable the optional watchdog FS-event layer (polling stays the baseline)",
        )
    return get_global_settings(db)
