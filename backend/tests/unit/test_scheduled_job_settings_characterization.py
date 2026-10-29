"""Characterization of the settings-store plumbing shared by three scheduled-job services.

``backup_service``, ``directory_sync_service`` and ``watch_settings_service`` each carried a
hand-copied ``_session`` context manager, bool/int parsers over the ``get_settings_map``
result, and a JSON ``last_result`` decode. These tests pin what callers observe through
each service's public ``get_settings`` / ``update_settings_last_run`` so the copies can be
folded into ``app.services.scheduled_job_settings`` without a behaviour change.

DB-free on purpose: the settings map is injected, so no shared ``system_settings`` row is
touched and no cross-worker lock ordering applies.
"""

from __future__ import annotations

import pytest

from app.core import constants as app_constants
from app.services import backup_service
from app.services import directory_sync_service
from app.services import system_settings_service
from app.services import watch_settings_service


@pytest.fixture
def settings_map(monkeypatch):
    """Inject the ``{key: str}`` map every service's ``get_settings`` reads."""
    store: dict[str, str | None] = {}
    monkeypatch.setattr(system_settings_service, "get_settings_map", lambda db, keys: dict(store))
    return store


_BOOL_SERVICES = [
    pytest.param(
        lambda db: backup_service.get_settings(db)["enabled"],
        "backup.enabled",
        app_constants.DEFAULT_BACKUP_ENABLED,
        id="backup",
    ),
    pytest.param(
        lambda db: directory_sync_service.get_settings(db)["enabled"],
        "directory_sync.enabled",
        app_constants.DEFAULT_DIRECTORY_SYNC_ENABLED,
        id="directory_sync",
    ),
    pytest.param(
        lambda db: watch_settings_service.get_global_settings(db)["enabled"],
        "watch.enabled",
        app_constants.DEFAULT_WATCH_ENABLED,
        id="watch",
    ),
]

_INT_SERVICES = [
    pytest.param(
        lambda db: backup_service.get_settings(db)["retention_daily"],
        "backup.retention_daily",
        app_constants.DEFAULT_BACKUP_RETENTION_DAILY,
        id="backup",
    ),
    pytest.param(
        lambda db: directory_sync_service.get_settings(db)["max_disables_per_run"],
        "directory_sync.max_disables_per_run",
        app_constants.DEFAULT_DIRECTORY_SYNC_MAX_DISABLES_PER_RUN,
        id="directory_sync",
    ),
    pytest.param(
        lambda db: watch_settings_service.get_global_settings(db)["file_stability_seconds"],
        "watch.file_stability_seconds",
        app_constants.DEFAULT_WATCH_FILE_STABILITY_SECONDS,
        id="watch",
    ),
]

_RESULT_SERVICES = [
    pytest.param(lambda db: backup_service.get_settings(db), "backup.last_result", id="backup"),
    pytest.param(
        lambda db: directory_sync_service.get_settings(db),
        "directory_sync.last_result",
        id="directory_sync",
    ),
]


@pytest.mark.parametrize(("read", "key", "default"), _BOOL_SERVICES)
@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on", "On"])
def test_bool_truthy_spellings_read_true(settings_map, read, key, default, raw):
    settings_map[key] = raw
    assert read(object()) is True


@pytest.mark.parametrize(("read", "key", "default"), _BOOL_SERVICES)
@pytest.mark.parametrize("raw", ["false", "0", "no", "off", "", "enabled"])
def test_bool_other_spellings_read_false(settings_map, read, key, default, raw):
    settings_map[key] = raw
    assert read(object()) is False


@pytest.mark.parametrize(("read", "key", "default"), _BOOL_SERVICES)
def test_bool_absent_key_reads_the_coded_default(settings_map, read, key, default):
    assert read(object()) is default


@pytest.mark.parametrize(("read", "key", "default"), _INT_SERVICES)
def test_int_stored_value_is_parsed(settings_map, read, key, default):
    settings_map[key] = str(default + 3)
    assert read(object()) == default + 3


@pytest.mark.parametrize(("read", "key", "default"), _INT_SERVICES)
@pytest.mark.parametrize("raw", ["abc", "7.5", ""])
def test_int_unparseable_value_falls_back_to_default(settings_map, read, key, default, raw):
    settings_map[key] = raw
    assert read(object()) == default


@pytest.mark.parametrize(("read", "key", "default"), _INT_SERVICES)
def test_int_absent_key_reads_the_coded_default(settings_map, read, key, default):
    assert read(object()) == default


@pytest.mark.parametrize(("read", "key"), _RESULT_SERVICES)
def test_last_result_json_object_is_decoded(settings_map, read, key):
    settings_map[key] = '{"ok": true, "n": 3}'
    assert read(object())["last_result"] == {"ok": True, "n": 3}


@pytest.mark.parametrize(("read", "key"), _RESULT_SERVICES)
@pytest.mark.parametrize("raw", ["{not json", "", None])
def test_last_result_garbled_or_empty_is_none(settings_map, read, key, raw):
    settings_map[key] = raw
    assert read(object())["last_result"] is None


@pytest.mark.parametrize(("read", "key"), _RESULT_SERVICES)
def test_last_result_absent_is_none(settings_map, read, key):
    assert read(object())["last_result"] is None


class _FakeSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def fake_session_local(monkeypatch):
    """Replace ``SessionLocal`` so ``db=None`` paths open (and we can see closed) a fake."""
    import app.db.base as db_base

    opened: list[_FakeSession] = []

    def factory():
        s = _FakeSession()
        opened.append(s)
        return s

    monkeypatch.setattr(db_base, "SessionLocal", factory)
    return opened


_GETTERS = [
    pytest.param(backup_service.get_settings, id="backup"),
    pytest.param(directory_sync_service.get_settings, id="directory_sync"),
    pytest.param(watch_settings_service.get_global_settings, id="watch"),
]


@pytest.mark.parametrize("getter", _GETTERS)
def test_no_session_opens_a_short_lived_one_and_closes_it(settings_map, fake_session_local, getter):
    getter(None)
    assert len(fake_session_local) == 1
    assert fake_session_local[0].closed is True


@pytest.mark.parametrize("getter", _GETTERS)
def test_a_passed_session_is_never_closed_or_replaced(settings_map, fake_session_local, getter):
    mine = _FakeSession()
    getter(mine)
    assert fake_session_local == []
    assert mine.closed is False


@pytest.mark.parametrize("getter", _GETTERS)
def test_the_short_lived_session_is_closed_when_the_read_raises(
    monkeypatch, fake_session_local, getter
):
    def boom(db, keys):
        raise RuntimeError("db down")

    monkeypatch.setattr(system_settings_service, "get_settings_map", boom)
    with pytest.raises(RuntimeError):
        getter(None)
    assert fake_session_local[0].closed is True


@pytest.mark.parametrize(
    ("stamp", "key"),
    [
        pytest.param(backup_service.update_settings_last_run, "backup.last_run_at", id="backup"),
        pytest.param(
            directory_sync_service.update_settings_last_run,
            "directory_sync.last_run_at",
            id="directory_sync",
        ),
    ],
)
def test_last_run_stamp_writes_the_iso_string_under_the_services_key(monkeypatch, stamp, key):
    writes: list[tuple[str, object]] = []
    monkeypatch.setattr(
        system_settings_service,
        "set_setting",
        lambda db, k, v, description=None: writes.append((k, v)),
    )
    stamp(object(), "2026-10-29T01:02:03+00:00")
    assert writes == [(key, "2026-10-29T01:02:03+00:00")]
