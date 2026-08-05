"""Unit tests for the watch.max_imports_per_scan rename (issue #295).

The setting was named `watch.max_concurrent_imports`, which promised parallelism the code
never implemented — it is applied as `standalone[:max_imports]` and imports run serially
inline inside the scan task. The rename must not silently reset a value an administrator
already configured under the old key, so reads fall back to the legacy key.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.constants import DEFAULT_WATCH_MAX_IMPORTS_PER_SCAN
from app.services import watch_settings_service

# The fake settings store never touches the session, so an unconnected Session
# instance is enough to satisfy the signatures without hitting a database.
_DB = Session()


class _FakeSettingsStore:
    """Stands in for system_settings_service against an in-memory dict."""

    def __init__(self, values: dict[str, str]):
        self.values = values
        self.writes: dict[str, object] = {}

    def get_setting_int(self, db, key, default=0):
        raw = self.values.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def get_setting_bool(self, db, key, default=False):
        raw = self.values.get(key)
        if raw is None:
            return default
        return raw.lower() in ("true", "1", "yes", "on")

    def get_settings_map(self, db, keys):
        return {k: self.values.get(k) for k in keys}

    def set_setting(self, db, key, value, description=None):
        self.writes[key] = value
        self.values[key] = str(value)


@pytest.fixture
def store(monkeypatch):
    fake = _FakeSettingsStore({})
    monkeypatch.setattr(watch_settings_service, "system_settings_service", fake)
    return fake


def test_new_key_is_read(store):
    store.values[watch_settings_service.KEY_MAX_IMPORTS_PER_SCAN] = "12"
    assert watch_settings_service.max_imports_per_scan(db=_DB) == 12


def test_legacy_key_is_honored_when_new_key_absent(store):
    """An upgrade must not silently reset a configured value to the coded default."""
    store.values[watch_settings_service.LEGACY_KEY_MAX_CONCURRENT_IMPORTS] = "9"
    assert watch_settings_service.max_imports_per_scan(db=_DB) == 9


def test_new_key_wins_over_legacy_key(store):
    store.values[watch_settings_service.LEGACY_KEY_MAX_CONCURRENT_IMPORTS] = "9"
    store.values[watch_settings_service.KEY_MAX_IMPORTS_PER_SCAN] = "3"
    assert watch_settings_service.max_imports_per_scan(db=_DB) == 3


def test_falls_back_to_coded_default_when_unset(store):
    assert watch_settings_service.max_imports_per_scan(db=_DB) == DEFAULT_WATCH_MAX_IMPORTS_PER_SCAN


def test_get_global_settings_exposes_new_field_name(store):
    store.values[watch_settings_service.KEY_MAX_IMPORTS_PER_SCAN] = "7"
    result = watch_settings_service.get_global_settings(db=_DB)

    assert result["max_imports_per_scan"] == 7
    assert "max_concurrent_imports" not in result


def test_get_global_settings_honors_legacy_key(store):
    store.values[watch_settings_service.LEGACY_KEY_MAX_CONCURRENT_IMPORTS] = "4"
    assert watch_settings_service.get_global_settings(db=_DB)["max_imports_per_scan"] == 4


def test_writes_only_target_the_new_key(store):
    """The legacy row must go inert on save, not be kept in sync."""
    watch_settings_service.update_global_settings(db=_DB, max_imports_per_scan=6)

    assert store.writes == {watch_settings_service.KEY_MAX_IMPORTS_PER_SCAN: 6}
    assert watch_settings_service.LEGACY_KEY_MAX_CONCURRENT_IMPORTS not in store.writes


def test_key_names_are_what_we_think_they_are():
    assert watch_settings_service.KEY_MAX_IMPORTS_PER_SCAN == "watch.max_imports_per_scan"
    assert (
        watch_settings_service.LEGACY_KEY_MAX_CONCURRENT_IMPORTS == "watch.max_concurrent_imports"
    )
