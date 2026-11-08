"""v469 migration + detection-arm consistency (audio tracks of a media file, issue #1263).

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

REVISION = "v469_add_media_file_audio_streams"
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
    name = f"ot_v469_{uuid.uuid4().hex[:10]}_test"
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
    assert scripts.get_revision(REVISION).down_revision == "v468_add_organization_owner"
    assert len(set(scripts.get_heads())) == 1


def test_detection_arm_returns_v469_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


def test_the_orm_mirrors_the_audio_columns(db_session):
    from app.db.base import Base

    conn = db_session.connection()
    live = {c["name"]: c for c in inspect(conn).get_columns("media_file")}
    assert set(live) == set(Base.metadata.tables["media_file"].columns.keys())
    assert live["audio_streams"]["nullable"] is True
    assert live["audio_stream_index"]["nullable"] is True


def test_upgrade_is_idempotent_keeps_rows_and_refuses_a_negative_index():
    module = _module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch, scratch.begin() as conn:
            conn.execute(text(_BASE_SQL))
            conn.execute(text("INSERT INTO media_file (filename) VALUES ('a'), ('b')"))
            conn.execute(text(module.UPGRADE_SQL))
            conn.execute(text(module.UPGRADE_SQL))

            rows = conn.execute(
                text("SELECT audio_streams, audio_stream_index FROM media_file")
            ).all()
            assert [tuple(r) for r in rows] == [(None, None), (None, None)]
            conn.execute(text("UPDATE media_file SET audio_stream_index = 1 WHERE id = 1"))
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(text("UPDATE media_file SET audio_stream_index = -1 WHERE id = 2"))


def test_the_downgrade_drops_columns_and_constraint_and_is_repeatable():
    module = _module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch, scratch.begin() as conn:
            conn.execute(text(_BASE_SQL))
            conn.execute(text(module.UPGRADE_SQL))
            conn.execute(text(module.DOWNGRADE_SQL))
            conn.execute(text(module.DOWNGRADE_SQL))

            cols = {c["name"] for c in inspect(conn).get_columns("media_file")}
            assert not cols & {"audio_streams", "audio_stream_index"}
            assert not conn.execute(
                text(
                    "SELECT 1 FROM pg_constraint WHERE conname = 'ck_media_file_audio_stream_index'"
                )
            ).first()
