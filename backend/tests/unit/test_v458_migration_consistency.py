"""v458 (speaker machine-identity partial index, issue #1328)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v458_speaker_machine_identity_index"
INDEX = "ix_speaker_machine_identity_media_file"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _index_def(conn) -> str | None:
    definition = conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE tablename = 'speaker' AND indexname = :n"),
        {"n": INDEX},
    ).scalar()
    return None if definition is None else str(definition)


def test_revision_chain_has_one_head():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(_PATH.parents[1]))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v457_audit_guard_followups"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_upgrade_builds_both_indexes_and_downgrade_drops_the_new_one():
    """Real Postgres: ``CONCURRENTLY`` needs its own autocommit connection, not the test session."""
    from sqlalchemy import create_engine

    from app.db.migration_connection import migration_database_url

    engine = create_engine(migration_database_url(), isolation_level="AUTOCOMMIT")
    m = _module()
    try:
        with engine.connect() as conn:
            conn.execute(text(f"DROP INDEX IF EXISTS {INDEX}"))
            assert _index_def(conn) is None
            conn.execute(text(m.CREATE_SQL))
            conn.execute(text(m.CREATE_SQL))  # idempotent
            definition = _index_def(conn)
            assert definition is not None
            assert "(media_file_id)" in definition
            assert "'voiceprint_link'" in definition
            media_idx = conn.execute(
                text(
                    "SELECT count(*) FROM pg_indexes WHERE tablename = 'speaker' "
                    "AND indexdef LIKE '%(media_file_id)' AND indexdef NOT LIKE '%WHERE%'"
                )
            ).scalar()
            assert media_idx == 1, "exactly one plain media_file_id index (no duplicate)"
            conn.execute(text(f"DROP INDEX IF EXISTS {INDEX}"))
            assert _index_def(conn) is None
            conn.execute(text(m.CREATE_SQL))  # leave the schema as the head expects
    finally:
        engine.dispose()


@pytest.mark.ddl_exclusive
def test_alembic_downgrade_drops_and_upgrade_rebuilds_through_the_runner():
    """The real revision path (``autocommit_block``), not just its SQL constants."""
    from sqlalchemy import create_engine

    from alembic import command
    from app.db.migration_connection import migration_database_url
    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(_PATH.parents[1]))
    engine = create_engine(migration_database_url(), isolation_level="AUTOCOMMIT")
    try:
        command.downgrade(config, "v457_audit_guard_followups")
        with engine.connect() as conn:
            assert _index_def(conn) is None
        command.upgrade(config, REVISION)
        with engine.connect() as conn:
            assert _index_def(conn) is not None
    finally:
        engine.dispose()


def test_invalid_leftover_is_dropped_and_a_valid_index_is_kept():
    from sqlalchemy import create_engine

    from app.db.migration_connection import migration_database_url

    engine = create_engine(migration_database_url(), isolation_level="AUTOCOMMIT")
    m = _module()
    try:
        with engine.connect() as conn:
            conn.execute(text(m.CREATE_SQL))
            conn.execute(text(m.DROP_INVALID_SQL))
            assert _index_def(conn) is not None, "a valid index must be left alone"
            # What a failed CONCURRENTLY build leaves behind (needs the owner/superuser role).
            conn.execute(
                text(
                    "UPDATE pg_index SET indisvalid = false WHERE indexrelid = CAST(:n AS regclass)"
                ),
                {"n": INDEX},
            )
            conn.execute(text(m.DROP_INVALID_SQL))
            assert _index_def(conn) is None
            conn.execute(text(m.CREATE_SQL))
            assert _index_def(conn) is not None
    finally:
        engine.dispose()


def test_model_declares_the_index():
    from app.db.base import Base
    from app.models.speaker import Speaker

    names = {i.name for i in Base.metadata.tables[Speaker.__tablename__].indexes}
    assert INDEX in names


def test_planner_uses_the_partial_index(db_session):
    """EXPLAIN on a seeded table. ``enable_seqscan = off`` is set for this session only: a few
    hundred rows are cheaper to scan than to index, so the planner would otherwise (correctly)
    pick a sequential scan and the test would say nothing about index usability."""
    conn = db_session.connection()
    assert _index_def(conn) is not None
    conn.execute(text("SET LOCAL enable_seqscan = off"))
    for shape in (
        "identity_source IN ('voiceprint', 'voiceprint_link')",
        "identity_source = 'voiceprint'",
        "identity_source = 'voiceprint_link'",
    ):
        plan = "\n".join(
            r[0]
            for r in conn.execute(
                text(f"EXPLAIN SELECT id FROM speaker WHERE media_file_id IN (1, 2, 3) AND {shape}")
            )
        )
        assert INDEX in plan, plan
    plan = "\n".join(
        r[0]
        for r in conn.execute(
            text(
                "EXPLAIN SELECT id FROM speaker WHERE media_file_id IN (1, 2, 3) "
                "AND identity_source = 'human'"
            )
        )
    )
    assert INDEX not in plan, "the partial index must not claim rows outside its predicate"


def test_detection_arm_returns_v458_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_index(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    # DROP INDEX is transactional, so the session transaction restores it on rollback.
    conn = db_session.connection()
    try:
        conn.execute(text(f"DROP INDEX IF EXISTS {INDEX}"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)
