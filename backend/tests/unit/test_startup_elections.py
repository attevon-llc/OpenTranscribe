"""Startup work that acts on SHARED state runs once per boot window, not once per replica.

docs/design/migrate_job_plan.md audits every step of the API lifespan. These are the steps that
were still running on every replica: the startup-recovery, thumbnail and imohash dispatches, the
shared-bucket bootstrap and quarantine backfill, and the legacy cache reclaims. Each must consult
``run_once_per_boot`` and do nothing when another replica already claimed it, while the
per-process parts next to them (the presign identity, the retention PUT) still run.
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest

from app.startup import maintenance


@pytest.fixture
def election(monkeypatch):
    """Control the election; record which steps asked."""
    asked: list[str] = []
    state = {"win": True}

    def run_once_per_boot(step: str, ttl: int = 300) -> bool:
        asked.append(step)
        return state["win"]

    monkeypatch.setattr("app.utils.boot_once.run_once_per_boot", run_once_per_boot)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(maintenance.asyncio, "sleep", no_sleep)
    return SimpleNamespace(asked=asked, state=state)


class _Task:
    def __init__(self, sink: list[str], name: str) -> None:
        self.sink, self.name = sink, name

    def delay(self, *args, **kwargs):
        self.sink.append(self.name)
        return SimpleNamespace(id=f"{self.name}-id")

    def apply_async(self, *args, **kwargs):
        return self.delay()


@pytest.mark.parametrize("win", [True, False])
def test_startup_recovery_dispatch_is_elected(election, monkeypatch, win):
    sent: list[str] = []
    monkeypatch.setattr("app.tasks.recovery.startup_recovery_task", _Task(sent, "recovery"))
    election.state["win"] = win

    asyncio.run(maintenance._run_startup_recovery())

    assert "startup_recovery_dispatch" in election.asked
    assert sent == (["recovery"] if win else [])


@pytest.mark.parametrize("win", [True, False])
def test_thumbnail_dispatch_is_elected(election, monkeypatch, win):
    sent: list[str] = []
    monkeypatch.setattr(
        "app.tasks.thumbnail_migration.migrate_thumbnails_to_webp", _Task(sent, "webp")
    )
    monkeypatch.setattr(
        "app.tasks.thumbnail_migration.backfill_missing_thumbnails", _Task(sent, "backfill")
    )
    monkeypatch.setattr("app.tasks.thumbnail_migration.one_time_pass_done", lambda key: False)
    election.state["win"] = win

    asyncio.run(maintenance._run_thumbnail_migration())

    assert "thumbnail_migration_dispatch" in election.asked
    assert sent == (["webp", "backfill"] if win else [])


class _NoFlagSession:
    """A session whose system_settings lookup finds nothing (the recompute has not finished)."""

    def query(self, *_):
        return self

    def filter(self, *_):
        return self

    def first(self):
        return None

    def close(self) -> None:
        return None


@pytest.mark.parametrize("win", [True, False])
def test_imohash_recompute_dispatch_is_elected(election, monkeypatch, win):
    sent: list[str] = []
    monkeypatch.setattr("app.db.base.SessionLocal", _NoFlagSession)
    monkeypatch.setattr("app.tasks.imohash_recompute.recompute_all", _Task(sent, "imohash"))
    election.state["win"] = win

    asyncio.run(maintenance._run_imohash_recompute())

    assert "imohash_recompute_dispatch" in election.asked
    assert sent == (["imohash"] if win else [])


@pytest.mark.parametrize("win", [True, False])
def test_shared_bucket_setup_is_elected_but_the_presign_identity_is_not(election, monkeypatch, win):
    ran: list[str] = []
    monkeypatch.setattr("app.services.storage_backend.is_native_s3", lambda: False)
    monkeypatch.setattr(maintenance, "_setup_media_bucket", lambda *a: ran.append("bucket"))
    monkeypatch.setattr(maintenance, "_ensure_presign_identity", lambda *a: ran.append("presign"))
    monkeypatch.setattr(maintenance, "_backfill_quarantine_tags", lambda: ran.append("backfill"))
    monkeypatch.setattr(
        maintenance, "_remove_public_bucket_policy", lambda *a: ran.append("policy")
    )
    election.state["win"] = win

    maintenance._setup_minio()

    assert "media_bucket_setup" in election.asked
    # Every replica signs URLs with its own presign identity; only the winner touches the bucket.
    assert ran == (["bucket", "presign", "backfill", "policy"] if win else ["presign"])


class _Closable:
    def close(self) -> None:
        return None


@pytest.mark.parametrize("win", [True, False])
def test_legacy_cache_reclaims_are_elected_but_retention_is_not(election, monkeypatch, win):
    ran: list[str] = []

    def apply_retention(db) -> int:
        ran.append("retention")
        return 7

    def reclaim_legacy_derived_cache() -> int:
        ran.append("derived")
        return 0

    cache = SimpleNamespace(
        apply_retention=apply_retention,
        reclaim_legacy_derived_cache=reclaim_legacy_derived_cache,
        reclaim_legacy_audio_cache_once=lambda db: ran.append("audio"),
    )
    flags = SimpleNamespace(
        get_setting_bool=lambda db, key, default: False, set_setting=lambda *a, **k: None
    )
    # Loaded first so the patch replaces the package attribute the step imports.
    importlib.import_module("app.services.cache_management_service")
    importlib.import_module("app.services.system_settings_service")
    monkeypatch.setattr("app.db.base.SessionLocal", _Closable)
    monkeypatch.setattr("app.services.cache_management_service", cache)
    monkeypatch.setattr("app.services.system_settings_service", flags)
    election.state["win"] = win

    maintenance._apply_derived_cache_policy()

    assert "legacy_cache_reclaim" in election.asked
    assert ran == (["retention", "derived", "audio"] if win else ["retention"])
