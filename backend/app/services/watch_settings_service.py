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
from app.core.constants import DEFAULT_WATCH_FS_EVENTS_MODE
from app.core.constants import DEFAULT_WATCH_FS_EVENTS_POLL_SECONDS
from app.core.constants import DEFAULT_WATCH_MAX_IMPORTS_PER_SCAN
from app.core.constants import WATCH_FS_EVENTS_MODES
from app.services import system_settings_service

logger = logging.getLogger(__name__)

KEY_ENABLED = "watch.enabled"
KEY_FILE_STABILITY_SECONDS = "watch.file_stability_seconds"
KEY_MAX_IMPORTS_PER_SCAN = "watch.max_imports_per_scan"
KEY_FS_EVENTS_ENABLED = "watch.fs_events_enabled"
KEY_FS_EVENTS_MODE = "watch.fs_events_mode"
KEY_FS_EVENTS_POLL_SECONDS = "watch.fs_events_poll_seconds"

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


def normalize_fs_events_mode(value: str | None) -> str:
    """Coerce a stored/submitted observer mode to a known value.

    Anything unrecognised falls back to ``auto`` — the mode that degrades on
    its own — so a typo in the DB can never wedge the watcher into a bad state.
    """
    candidate = (value or "").strip().lower()
    if candidate in WATCH_FS_EVENTS_MODES:
        return candidate
    return DEFAULT_WATCH_FS_EVENTS_MODE


def fs_events_mode(db: Session | None = None) -> str:
    """Observer selection mode: ``auto`` | ``native`` | ``polling`` | ``off``."""
    with _session(db) as s:
        return normalize_fs_events_mode(
            system_settings_service.get_setting(s, KEY_FS_EVENTS_MODE, DEFAULT_WATCH_FS_EVENTS_MODE)
        )


def fs_events_poll_seconds(db: Session | None = None) -> int:
    """Stat-sweep interval used by the PollingObserver fallback (>= 1 s)."""
    with _session(db) as s:
        return max(
            1,
            system_settings_service.get_setting_int(
                s, KEY_FS_EVENTS_POLL_SECONDS, DEFAULT_WATCH_FS_EVENTS_POLL_SECONDS
            ),
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
                KEY_FS_EVENTS_MODE,
                KEY_FS_EVENTS_POLL_SECONDS,
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
            "fs_events_mode": normalize_fs_events_mode(vals.get(KEY_FS_EVENTS_MODE)),
            "fs_events_poll_seconds": max(
                1, _i(KEY_FS_EVENTS_POLL_SECONDS, DEFAULT_WATCH_FS_EVENTS_POLL_SECONDS)
            ),
        }


def update_global_settings(
    db: Session,
    *,
    enabled: bool | None = None,
    file_stability_seconds: int | None = None,
    max_imports_per_scan: int | None = None,
    fs_events_enabled: bool | None = None,
    fs_events_mode: str | None = None,
    fs_events_poll_seconds: int | None = None,
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
    if fs_events_mode is not None:
        system_settings_service.set_setting(
            db,
            KEY_FS_EVENTS_MODE,
            normalize_fs_events_mode(fs_events_mode),
            "FS-event observer mode: auto | native | polling | off",
        )
    if fs_events_poll_seconds is not None:
        system_settings_service.set_setting(
            db,
            KEY_FS_EVENTS_POLL_SECONDS,
            max(1, int(fs_events_poll_seconds)),
            "Stat-sweep interval (s) for the PollingObserver fallback",
        )
    return get_global_settings(db)
