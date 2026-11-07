"""Two super admins turning the usage report on at once both succeed, with one install id.

Real PostgreSQL connections, one per thread, on their own ``NullPool`` engine (the pattern and
the reason for the engine are in ``test_llm_token_limits_race.py``). Both racers are held at
the moment they create an id, so each has already decided none exists before either writes
it: a read-then-insert implementation fails the second writer on ``system_settings_key_key``.

These sessions commit for real, so the telemetry rows are deleted afterwards and the module
shares the ``telemetry_setting`` xdist group with every other test that touches them.
"""

from __future__ import annotations

import threading
import types
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core import settings_cache
from app.db.base import SessionLocal
from app.db.base import engine as app_engine
from app.models.system_settings import SystemSettings
from app.services import telemetry

pytestmark = pytest.mark.xdist_group("telemetry_setting")

_KEYS = list(telemetry._SETTING_KEYS)


def _clear() -> None:
    with SessionLocal() as db:
        db.execute(delete(SystemSettings).where(SystemSettings.key.in_(_KEYS)))
        db.commit()
    for key in _KEYS:
        settings_cache.invalidate(key)


@pytest.fixture
def racers(monkeypatch):
    for name in ("DO_NOT_TRACK", "OPENTRANSCRIBE_TELEMETRY", "OPENTRANSCRIBE_DEMO_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENTRANSCRIBE_TELEMETRY_PROJECT_KEY", "phc_test_key")
    with SessionLocal() as db:
        present = db.scalars(select(SystemSettings.key).where(SystemSettings.key.in_(_KEYS))).all()
    assert present == [], f"the test database already holds telemetry rows: {present}"
    engine = create_engine(app_engine.url, poolclass=NullPool)
    yield sessionmaker(bind=engine)
    engine.dispose()
    _clear()


def _hold_id_creation(monkeypatch, threads: int) -> None:
    """Every racer reaches the point of creating an id before any of them writes one."""
    barrier = threading.Barrier(threads)

    def uuid4() -> uuid.UUID:
        barrier.wait(timeout=10)
        return uuid.uuid4()

    monkeypatch.setattr(telemetry, "uuid", types.SimpleNamespace(uuid4=uuid4, UUID=uuid.UUID))


def test_two_concurrent_enables_both_succeed_and_share_one_install_id(racers, monkeypatch):
    _hold_id_creation(monkeypatch, threads=2)
    outcomes: list[str] = []
    ids: list[str | None] = []
    lock = threading.Lock()

    def enable() -> None:
        with racers() as db:
            try:
                telemetry.set_enabled(db, True)
                seen = telemetry.read_state(db)["install_id"]
                outcome = "ok"
            except Exception as exc:  # recorded, not raised: an assert in a thread is lost
                seen, outcome = None, type(exc).__name__
        with lock:
            outcomes.append(outcome)
            ids.append(seen)

    pool = [threading.Thread(target=enable) for _ in range(2)]
    for t in pool:
        t.start()
    for t in pool:
        t.join(timeout=60)

    with SessionLocal() as db:
        state = telemetry.read_state(db)
    assert outcomes == ["ok", "ok"], outcomes
    assert state["enabled"] is True
    assert ids == [state["install_id"], state["install_id"]]
    assert uuid.UUID(state["install_id"]).version == 4
