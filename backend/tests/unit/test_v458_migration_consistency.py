"""v458 (speaker machine-identity partial index, issue #1328)."""

from __future__ import annotations

import importlib.util
import logging
import re
import time
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


def _engine():
    from sqlalchemy import create_engine

    from app.db.migration_connection import migration_database_url

    return create_engine(migration_database_url())


def _index_def(conn) -> str | None:
    definition = conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE tablename = 'speaker' AND indexname = :n"),
        {"n": INDEX},
    ).scalar()
    return None if definition is None else str(definition)


def _valid(conn) -> bool | None:
    value = conn.execute(
        text(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = :n"
        ),
        {"n": INDEX},
    ).scalar()
    return None if value is None else bool(value)


def _drop(conn) -> None:
    conn.execute(text(f"DROP INDEX IF EXISTS {INDEX}"))


def _build(**kw):
    from app.db import concurrent_indexes
    from app.db.speaker_indexes import SPEAKER_INDEXES

    with _engine().connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        return concurrent_indexes.ensure_concurrently(conn, SPEAKER_INDEXES, **kw)


@pytest.fixture
def index_absent():
    """Run with the index absent; rebuild it afterwards so the schema matches the head."""
    engine = _engine()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        _drop(conn)
    try:
        yield engine
    finally:
        _build()
        engine.dispose()


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


def test_the_revision_itself_issues_no_ddl():
    """A revision that built the index inside the upgrade could hang startup (see its docstring)."""
    m = _module()
    assert not re.search(r"CREATE|DROP|execute", m.upgrade.__code__.co_names.__repr__(), re.I)
    assert "CONCURRENTLY" not in _PATH.read_text().split("def upgrade")[1].split("def downgrade")[0]


def test_the_shared_builder_creates_the_partial_index_idempotently(index_absent):
    assert _build() == {INDEX: "built"}
    with index_absent.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        definition = _index_def(conn)
        assert definition is not None
        assert "(media_file_id)" in definition and "'voiceprint_link'" in definition
        plain = conn.execute(
            text(
                "SELECT count(*) FROM pg_indexes WHERE tablename = 'speaker' "
                "AND indexdef LIKE '%(media_file_id)' AND indexdef NOT LIKE '%WHERE%'"
            )
        ).scalar()
        assert plain == 1, "exactly one plain media_file_id index (no duplicate)"
    assert _build() == {INDEX: "exists"}


def test_an_invalid_leftover_is_dropped_and_rebuilt(index_absent):
    assert _build() == {INDEX: "built"}
    with index_absent.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        # What a failed CONCURRENTLY build leaves behind (needs the owner/superuser role).
        conn.execute(
            text("UPDATE pg_index SET indisvalid = false WHERE indexrelid = CAST(:n AS regclass)"),
            {"n": INDEX},
        )
    assert _build() == {INDEX: "built"}
    with index_absent.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        assert _index_def(conn) is not None


def test_the_downgrade_drops_the_index(index_absent):
    from alembic import command
    from app.db.migrations import get_alembic_config

    assert _build() == {INDEX: "built"}
    config = get_alembic_config()
    config.set_main_option("script_location", str(_PATH.parents[1]))
    command.downgrade(config, "v457_audit_guard_followups")
    with index_absent.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        assert _index_def(conn) is None
    command.upgrade(config, "head")


def test_a_build_blocked_by_an_open_transaction_gives_up_without_raising(index_absent):
    with index_absent.connect().execution_options(isolation_level="REPEATABLE READ") as blocker:
        blocker.execute(text("SELECT count(*) FROM speaker"))
        started = time.monotonic()
        outcome = _build(lock_timeout_seconds=1)
        waited = time.monotonic() - started
        blocker.rollback()
    assert outcome == {INDEX: "failed"}
    assert waited < 30
    assert _build() == {INDEX: "built"}


@pytest.mark.ddl_exclusive
def test_run_migrations_builds_the_index_and_survives_a_held_transaction(
    index_absent, monkeypatch, caplog
):
    """The real startup path (advisory-lock connection and all)."""
    from app.db import concurrent_indexes
    from app.db import migrations

    monkeypatch.setattr(concurrent_indexes, "BUILD_LOCK_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(migrations, "_speaker_index_retry_done", False)
    with index_absent.connect().execution_options(isolation_level="REPEATABLE READ") as blocker:
        blocker.execute(text("SELECT count(*) FROM speaker"))
        started = time.monotonic()
        with caplog.at_level(logging.WARNING):
            migrations.run_migrations()
        assert time.monotonic() - started < 120, "startup waited on the held transaction"
        blocker.rollback()
    with index_absent.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        # The abandoned build may leave an INVALID index (its cleanup waits on the same blocker);
        # the next start drops and rebuilds it. What matters: no VALID index was forced through.
        assert _valid(conn) is not True, "the build must have been abandoned, not forced"
    assert any("still pending" in r.getMessage() for r in caplog.records)

    monkeypatch.setattr(migrations, "_speaker_index_retry_done", False)
    migrations.run_migrations()
    with index_absent.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        assert _valid(conn) is True


def test_model_declares_the_index():
    from app.db.base import Base
    from app.models.speaker import Speaker

    names = {i.name for i in Base.metadata.tables[Speaker.__tablename__].indexes}
    assert INDEX in names


def test_planner_uses_the_partial_index(db_session):
    """EXPLAIN on the (nearly empty) test schema. ``enable_seqscan = off`` is set for this
    transaction only: a few rows are cheaper to scan than to index, so the planner would
    otherwise (correctly) choose a sequential scan and say nothing about index usability."""
    _build()
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

    _build()
    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_v457_while_the_index_is_pending(db_session):
    """Stamping low is the documented, harmless outcome: the revision has no DDL to skip."""
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f"DROP INDEX IF EXISTS {INDEX}"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)
