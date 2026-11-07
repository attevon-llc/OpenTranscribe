"""v465 migration + detection-arm consistency (per-user hide of media files, #1102).

The idempotency and downgrade checks run the revision's real SQL in a PRIVATE scratch database
(precedent: ``test_v464_migration_consistency.py``), so no DDL touches the shared suite database;
each takes the suite DDL lock EXCLUSIVE, which is what ``test_ddl_marker_discipline`` looks for in
a test that runs DDL without ``db_session``.
"""

from __future__ import annotations

import importlib.util
import re
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

REVISION = "v465_add_user_hidden_media"
TABLE = "user_hidden_media"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"

#: The two parents the table references, reduced to their keys, for the scratch database.
_PARENTS_SQL = """
CREATE TABLE "user" (id serial PRIMARY KEY);
CREATE TABLE media_file (id serial PRIMARY KEY);
INSERT INTO "user" (id) VALUES (1), (2);
INSERT INTO media_file (id) VALUES (10), (11);
"""


def _revision_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@contextmanager
def _scratch_database() -> Iterator[Engine]:
    """An empty database of our own on the test server."""
    name = f"ot_v465_{uuid.uuid4().hex[:10]}_test"
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


def _hide(conn, user_id: int, file_id: int) -> None:
    conn.execute(
        text("INSERT INTO user_hidden_media (user_id, media_file_id) VALUES (:u, :f)"),
        {"u": user_id, "f": file_id},
    )


def _rows(conn) -> set[tuple[int, int]]:
    rows = conn.execute(text("SELECT user_id, media_file_id FROM user_hidden_media")).all()
    return {(r[0], r[1]) for r in rows}


# --- no database -----------------------------------------------------------------------------


def test_revision_chain_has_one_head():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(_PATH.parents[1]))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v464_generalise_erasure_grace_index"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


# --- the migrated suite database -------------------------------------------------------------


def test_detection_arm_returns_v465_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


def test_the_orm_mirrors_the_table_and_both_parents_cascade(db_session):
    from app.db.base import Base

    conn = db_session.connection()
    live = {c["name"] for c in inspect(conn).get_columns(TABLE)}
    assert live == set(Base.metadata.tables[TABLE].columns.keys())
    rules = conn.execute(
        text(
            "SELECT confrelid::regclass::text, confdeltype FROM pg_constraint "
            "WHERE conrelid = CAST(:t AS regclass) AND contype = 'f'"
        ),
        {"t": TABLE},
    ).all()
    assert {(parent.strip('"'), rule) for parent, rule in rules} == {
        ("user", "c"),
        ("media_file", "c"),
    }
    assert "ix_user_hidden_media_media_file" in {
        i["name"] for i in inspect(conn).get_indexes(TABLE)
    }


# --- private scratch database: idempotent upgrade, uniqueness, cascades, downgrade ------------


def test_one_row_per_user_and_file_and_both_parents_cascade():
    module = _revision_module()
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch, scratch.begin() as conn:
            conn.execute(text(_PARENTS_SQL))
            conn.execute(text(module.UPGRADE_SQL))
            _hide(conn, 1, 10)
            _hide(conn, 2, 10)
            _hide(conn, 1, 11)
            with pytest.raises(IntegrityError, match="pk_user_hidden_media"), conn.begin_nested():
                _hide(conn, 1, 10)

            conn.execute(text("DELETE FROM media_file WHERE id = 10"))
            assert _rows(conn) == {(1, 11)}, "a purged file must take every user's hide rows"
            conn.execute(text('DELETE FROM "user" WHERE id = 1'))
            assert _rows(conn) == set(), "a deleted account must leave no hide rows"


def test_the_upgrade_is_idempotent_and_the_downgrade_drops_only_the_table():
    module = _revision_module()
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch:
            with scratch.begin() as conn:
                conn.execute(text(_PARENTS_SQL))
                conn.execute(text(module.UPGRADE_SQL))
                conn.execute(text(module.UPGRADE_SQL))
                assert TABLE in inspect(conn).get_table_names()
                _hide(conn, 1, 10)
            with scratch.begin() as conn:
                conn.execute(text(module.DOWNGRADE_SQL))
                conn.execute(text(module.DOWNGRADE_SQL))
                names = set(inspect(conn).get_table_names())
                assert TABLE not in names
                assert {"user", "media_file"} <= names
