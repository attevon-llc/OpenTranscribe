"""v471 migration + detection-arm consistency (``owner_since`` on every table a transfer re-owns).

The idempotency and downgrade checks run the revision's real SQL in a PRIVATE scratch database
(precedent: ``test_v470_migration_consistency.py``); each takes the suite DDL lock EXCLUSIVE.
"""

from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy import text

from app.db.base import engine as shared_engine
from tests.db_locks import acquire_ddl_lock_exclusive
from tests.unit._migration_detection import assert_follows_with_single_head
from tests.unit._migration_detection import load_revision_module
from tests.unit._migration_detection import scratch_connection

REVISION = "v471_add_owner_since"
DOWN_REVISION = "v470_add_requested_source_language"


def _base_sql(tables: tuple[str, ...]) -> str:
    return "".join(f"CREATE TABLE {t} (id serial PRIMARY KEY);" for t in tables)


def test_revision_chain_has_one_head():
    assert_follows_with_single_head(REVISION, DOWN_REVISION)


def test_detection_arm_returns_v471_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


def test_every_model_the_transfer_re_owns_has_the_column_and_the_revision_adds_it():
    """A table the transfer starts re-owning without ``owner_since`` would be judged by its
    creation time again; the revision, the ORM and the transfer must name the same tables."""
    from app.services import org_content_transfer as transfer

    moved = {
        model.__tablename__ for _key, model, _column in (*transfer._FILE_TABLES, *transfer._TABLES)
    } | {"media_file"}
    assert set(load_revision_module(REVISION).TABLES) == moved
    for _key, model, _column in (*transfer._FILE_TABLES, *transfer._TABLES):
        assert "owner_since" in model.__table__.columns, model.__tablename__


def test_upgrade_is_idempotent_and_keeps_rows_null():
    module = load_revision_module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with scratch_connection("ot_v471") as conn:
            conn.execute(text(_base_sql(module.TABLES)))
            conn.execute(text("INSERT INTO media_file DEFAULT VALUES"))
            conn.execute(text(module.UPGRADE_SQL))
            conn.execute(text(module.UPGRADE_SQL))

            for table in module.TABLES:
                cols = {c["name"]: c for c in inspect(conn).get_columns(table)}
                assert cols["owner_since"]["nullable"] is True, table
            rows = conn.execute(text("SELECT owner_since FROM media_file")).all()
            assert [tuple(r) for r in rows] == [(None,)]


def test_the_downgrade_drops_every_column_and_is_repeatable():
    module = load_revision_module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with scratch_connection("ot_v471") as conn:
            conn.execute(text(_base_sql(module.TABLES)))
            conn.execute(text(module.UPGRADE_SQL))
            conn.execute(text(module.DOWNGRADE_SQL))
            conn.execute(text(module.DOWNGRADE_SQL))

            still_there = {
                table
                for table in module.TABLES
                if "owner_since" in {c["name"] for c in inspect(conn).get_columns(table)}
            }
            assert len(module.TABLES) == 13
            assert still_there == set()
