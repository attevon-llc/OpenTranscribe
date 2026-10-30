"""Untracked-database detection on a REAL Postgres at a pre-v091 schema.

``v439``'s data probe reads ``speaker.suggestion_source`` (added by ``v091``). A database
migrated only to v090 has ``speaker``, ``speaker_profile`` and ``speaker_match`` but not that
column, and the probe used to raise ``UndefinedColumn`` there, so ``run_migrations`` failed on
any untracked v010-v090 database. The unit fake models the same shape
(``tests/unit/test_schema_detection_data_probe_columns.py``); this proves it against the
engine that actually raises. It runs the real Alembic chain on the throwaway container of
``test_v436_lite_settings_migration`` (loopback port, tmpfs, uuid-named, removed in ``finally``).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

from tests.integration import test_v436_lite_settings_migration as _pg_harness

pytestmark = pytest.mark.integration

# Re-exported by assignment so pytest finds the container fixture in this module.
throwaway_pg = _pg_harness.throwaway_pg

V090 = "v090_add_error_category"
V091 = "v091_add_speaker_suggestion_source"


def _upgrade(pg, db: str, revision: str) -> None:
    """``python -m alembic upgrade`` in a child process, as production runs it.

    Not in-process: ``env.py`` reads the already-imported ``settings`` object, which points
    at whatever database this pytest process was configured with, not at the throwaway one.
    """
    env = {
        **os.environ,
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": str(pg["port"]),
        "POSTGRES_USER": _pg_harness._DB_USER,
        "POSTGRES_PASSWORD": pg["password"],
        "POSTGRES_DB": db,
        "DEPLOYMENT_MODE": "full",
    }
    done = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=_pg_harness._BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]


def _detect_untracked(engine) -> str | None:
    """Drop the version table (an untracked database) and run the real detector."""
    from app.db.migrations import _detect_schema_version

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
    with engine.connect() as conn:
        return _detect_schema_version(conn, inspect(conn).get_table_names())


@pytest.mark.parametrize("target", [V090, V091])
def test_an_untracked_legacy_database_is_detected_not_crashed_on(throwaway_pg, target):
    from tests.unit._migration_detection import _chain_order

    pg = throwaway_pg
    db, engine = _pg_harness._new_database(pg, f"legacy_{target[:4]}")
    try:
        _upgrade(pg, db, target)
        with engine.connect() as conn:
            has_column = conn.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='speaker' AND column_name='suggestion_source')"
                )
            ).scalar()
        assert has_column is (target == V091)

        detected = _detect_untracked(engine)

        # Which arm wins is the ladder's business (the baseline already carries the v100-v120
        # indexes, so a v090 database legitimately reads as v120); this test pins that the
        # question is answerable at all instead of raising out of run_migrations.
        assert detected in _chain_order()
    finally:
        engine.dispose()
