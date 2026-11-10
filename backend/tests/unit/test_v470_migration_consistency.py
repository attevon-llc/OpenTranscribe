"""v470 migration + detection-arm consistency (the language forced for one run, issue #1263).

The idempotency and downgrade checks run the revision's real SQL in a PRIVATE scratch database
(precedent: ``test_v468_migration_consistency.py``); each takes the suite DDL lock EXCLUSIVE.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.base import engine as shared_engine
from tests.db_locks import acquire_ddl_lock_exclusive
from tests.unit._migration_detection import assert_follows_with_single_head
from tests.unit._migration_detection import load_revision_module
from tests.unit._migration_detection import scratch_connection

REVISION = "v470_add_requested_source_language"
DOWN_REVISION = "v469_add_media_file_audio_streams"

_BASE_SQL = "CREATE TABLE media_file (id serial PRIMARY KEY, filename text);"


def test_revision_chain_has_one_head():
    assert_follows_with_single_head(REVISION, DOWN_REVISION)


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
    module = load_revision_module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with scratch_connection("ot_v470") as conn:
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
    module = load_revision_module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with scratch_connection("ot_v470") as conn:
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
