"""The API process bounds every statement with Postgres ``statement_timeout``.

Nothing set one anywhere (#1337 M2 #5: API transactions of 120-292 s of short
statements, and ``profiles.list`` running 108-266 s against a 60 s client timeout).
``DB_STATEMENT_TIMEOUT_MS`` is applied per session by a pool ``connect`` event, the way
the idle-in-transaction backstop already is; the coded default is 0 so only the API
service, whose compose environment sets it, gets one.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text

from app.db.base import engine as app_engine

pytestmark = pytest.mark.unit

BACKEND = Path(__file__).resolve().parents[2]


def _show_statement_timeout(timeout_ms: int) -> str:
    from app.db.migration_connection import install_statement_timeout

    eng = install_statement_timeout(create_engine(app_engine.url), timeout_ms)
    try:
        with eng.connect() as conn:
            return str(conn.execute(text("SHOW statement_timeout")).scalar())
    finally:
        eng.dispose()


def test_a_configured_timeout_is_set_on_every_connection():
    assert _show_statement_timeout(1500) == "1500ms"


def test_zero_installs_nothing():
    assert _show_statement_timeout(0) == "0"


def test_the_coded_default_is_off():
    """Celery workers and scripts import the same engine and must stay unbounded."""
    from app.core.settings_capacity import CapacitySettings

    assert CapacitySettings.model_fields["DB_STATEMENT_TIMEOUT_MS"].default == 0


def test_the_api_engine_applies_the_setting():
    """``app.db.base`` wires ``DB_STATEMENT_TIMEOUT_MS`` into the shared engine."""
    probe = (
        "from sqlalchemy import text\n"
        "from app.db.base import engine\n"
        "with engine.connect() as c:\n"
        "    print(c.execute(text('SHOW statement_timeout')).scalar())\n"
    )
    env = {**os.environ, "DB_STATEMENT_TIMEOUT_MS": "1500"}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "1500ms"


def test_the_compose_backend_service_sets_it_and_no_worker_does():
    yaml = pytest.importorskip("yaml")
    services = yaml.safe_load((BACKEND.parent / "docker-compose.yml").read_text())["services"]

    def env_keys(service: dict) -> set[str]:
        env = service.get("environment") or []
        if isinstance(env, dict):
            return set(env)
        return {item.split("=", 1)[0] for item in env}

    assert "DB_STATEMENT_TIMEOUT_MS" in env_keys(services["backend"])
    others = sorted(
        name
        for name, svc in services.items()
        if name != "backend" and "DB_STATEMENT_TIMEOUT_MS" in env_keys(svc)
    )
    assert others == []


def test_a_negative_value_is_floored_to_off(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "-5")
    assert Settings().DB_STATEMENT_TIMEOUT_MS == 0
