"""v470 migration + detection-arm consistency (the language forced for one run, issue #1263).

The idempotency and downgrade checks run the revision's real SQL in a PRIVATE scratch database
(precedent: ``test_v468_migration_consistency.py``); each takes the suite DDL lock EXCLUSIVE.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.base import engine as shared_engine
from tests.db_locks import acquire_ddl_lock_exclusive

REVISION = "v470_add_requested_source_language"
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"

_BASE_SQL = "CREATE TABLE media_file (id serial PRIMARY KEY, filename text);"


def _module(revision: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(revision, _VERSIONS / f"{revision}.py")
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@contextmanager
def _scratch_database() -> Iterator[Engine]:
    name = f"ot_v470_{uuid.uuid4().hex[:10]}_test"
    url = make_url(settings.DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    scratch = create_engine(url.set(database=name))
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield scratch
    finally:
        scratch.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_revision_chain_has_one_head():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(_VERSIONS.parent))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v469_add_media_file_audio_streams"
    assert len(set(scripts.get_heads())) == 1


def test_detection_arm_returns_v470_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


def test_the_orm_mirrors_the_column_and_its_check(db_session):
    from app.db.base import Base

    conn = db_session.connection()
    live = {c["name"]: c for c in inspect(conn).get_columns("media_file")}
    assert set(live) == set(Base.metadata.tables["media_file"].columns.keys())
    assert live["requested_source_language"]["nullable"] is True
    orm_checks = {
        c.name for c in Base.metadata.tables["media_file"].constraints if c.name is not None
    }
    assert "ck_media_file_requested_source_language" in orm_checks


def test_upgrade_is_idempotent_keeps_rows_and_refuses_a_malformed_code():
    module = _module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch, scratch.begin() as conn:
            conn.execute(text(_BASE_SQL))
            conn.execute(text("INSERT INTO media_file (filename) VALUES ('a'), ('b')"))
            conn.execute(text(module.UPGRADE_SQL))
            conn.execute(text(module.UPGRADE_SQL))

            rows = conn.execute(text("SELECT requested_source_language FROM media_file")).all()
            assert [tuple(r) for r in rows] == [(None,), (None,)]
            for ok in ("fr", "auto", "haw"):
                conn.execute(
                    text("UPDATE media_file SET requested_source_language = :v WHERE id = 1"),
                    {"v": ok},
                )
            for bad in ("", "FR", "english", "f"):
                with pytest.raises(IntegrityError), conn.begin_nested():
                    conn.execute(
                        text("UPDATE media_file SET requested_source_language = :v WHERE id = 2"),
                        {"v": bad},
                    )


def test_the_downgrade_drops_column_and_constraint_and_is_repeatable():
    module = _module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch, scratch.begin() as conn:
            conn.execute(text(_BASE_SQL))
            conn.execute(text(module.UPGRADE_SQL))
            conn.execute(text(module.DOWNGRADE_SQL))
            conn.execute(text(module.DOWNGRADE_SQL))

            cols = {c["name"] for c in inspect(conn).get_columns("media_file")}
            assert "requested_source_language" not in cols
            assert not conn.execute(
                text(
                    "SELECT 1 FROM pg_constraint "
                    "WHERE conname = 'ck_media_file_requested_source_language'"
                )
            ).first()
