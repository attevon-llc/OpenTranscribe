"""v458 (speaker machine-identity partial index, issue #1328).

``CREATE INDEX CONCURRENTLY`` waits for every transaction of every backend in the SAME
database, and these tests deliberately hold transactions open. On the shared suite database that
races every other xdist worker, so everything that builds the index, holds a transaction or runs
``run_migrations()`` runs in a PRIVATE scratch database created per test (precedent:
``test_v030_ldap_migration_contract.py``). Each such test also takes the suite DDL lock
EXCLUSIVE on the shared database, which is what ``test_ddl_marker_discipline`` looks for in a
test that runs DDL without ``db_session``.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.db.base import engine as shared_engine
from tests.db_locks import acquire_ddl_lock_exclusive

REVISION = "v458_speaker_machine_identity_index"
INDEX = "ix_speaker_machine_identity_media_file"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"

#: The columns the index and its predicate touch; nothing else of ``speaker`` matters to it.
MINIMAL_SPEAKER = (
    "CREATE TABLE speaker (id SERIAL PRIMARY KEY, media_file_id INTEGER NOT NULL, "
    "identity_source VARCHAR(16))"
)


def _revision_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@contextmanager
def _scratch_database(starting_sql: str | None) -> Iterator[tuple[Engine, str]]:
    """A database of our own on the test server (empty when ``starting_sql`` is None)."""
    name = f"ot_v458_{uuid.uuid4().hex[:10]}_test"
    url = make_url(settings.DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    scratch = create_engine(url.set(database=name))
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        if starting_sql:
            with scratch.begin() as conn:
                conn.execute(text(starting_sql))
        yield scratch, scratch.url.render_as_string(hide_password=False)
    finally:
        scratch.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _autocommit(scratch: Engine):
    return scratch.connect().execution_options(isolation_level="AUTOCOMMIT")


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


def _build(scratch: Engine, lock_timeout_seconds: int | None = None):
    from app.db import concurrent_indexes
    from app.db.speaker_indexes import SPEAKER_INDEXES

    with _autocommit(scratch) as conn:
        return concurrent_indexes.ensure_concurrently(
            conn, SPEAKER_INDEXES, lock_timeout_seconds=lock_timeout_seconds
        )


class _Collect(logging.Handler):
    def __init__(self, sink: list[logging.LogRecord]) -> None:
        super().__init__(logging.WARNING)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self._sink.append(record)


# --- no database: identity, shape, source ---------------------------------------------------


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
    upgrade_body = _PATH.read_text().split("def upgrade")[1].split("def downgrade")[0]
    assert "CONCURRENTLY" not in upgrade_body and "execute" not in upgrade_body


def test_model_declares_the_index():
    from app.db.base import Base
    from app.models.speaker import Speaker

    names = {i.name for i in Base.metadata.tables[Speaker.__tablename__].indexes}
    assert INDEX in names


def test_the_spec_is_the_partial_concurrent_index_the_model_declares():
    from app.db.speaker_indexes import MACHINE_IDENTITY

    sql = MACHINE_IDENTITY.create_sql()
    assert sql.startswith(f"CREATE INDEX CONCURRENTLY {INDEX} ON speaker (media_file_id) WHERE ")
    assert "'voiceprint'" in sql and "'voiceprint_link'" in sql


# --- minimal scratch database: the builder, the planner, the downgrade ----------------------


def test_the_shared_builder_creates_the_partial_index_idempotently():
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database(MINIMAL_SPEAKER) as (scratch, _url):
            assert _build(scratch) == {INDEX: "built"}
            with scratch.connect() as conn:
                definition = _index_def(conn)
                assert definition is not None
                assert "(media_file_id)" in definition and "'voiceprint_link'" in definition
            assert _build(scratch) == {INDEX: "exists"}


def test_an_invalid_leftover_is_dropped_and_rebuilt():
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database(MINIMAL_SPEAKER) as (scratch, _url):
            assert _build(scratch) == {INDEX: "built"}
            with _autocommit(scratch) as conn:
                # What a failed CONCURRENTLY build leaves behind (needs the owner role).
                conn.execute(
                    text(
                        "UPDATE pg_index SET indisvalid = false "
                        "WHERE indexrelid = CAST(:n AS regclass)"
                    ),
                    {"n": INDEX},
                )
                assert _valid(conn) is False
            assert _build(scratch) == {INDEX: "built"}
            with scratch.connect() as conn:
                assert _valid(conn) is True


def test_a_build_blocked_by_an_open_transaction_gives_up_without_raising():
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database(MINIMAL_SPEAKER) as (scratch, _url):
            with scratch.connect().execution_options(isolation_level="REPEATABLE READ") as blocker:
                blocker.execute(text("SELECT count(*) FROM speaker"))
                started = time.monotonic()
                outcome = _build(scratch, lock_timeout_seconds=1)
                waited = time.monotonic() - started
                blocker.rollback()
            assert outcome == {INDEX: "failed"}
            assert waited < 30
            assert _build(scratch) == {INDEX: "built"}


def test_the_planner_uses_the_partial_index_for_voiceprint_lookups():
    """EXPLAIN on a seeded scratch table (its own index set, so shared contents cannot matter).

    ``enable_seqscan = off`` is set for the session: even on 30k rows the planner may prefer a
    sequential scan for the ``IN`` list, and this test is about whether the index is USABLE for
    each query shape, not about the cost model.
    """
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database(MINIMAL_SPEAKER) as (scratch, _url):
            with scratch.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO speaker (media_file_id, identity_source) "
                        "SELECT g % 3000, CASE WHEN g % 50 = 0 THEN 'voiceprint' "
                        "WHEN g % 50 = 1 THEN 'voiceprint_link' ELSE 'human' END "
                        "FROM generate_series(1, 30000) g"
                    )
                )
            assert _build(scratch) == {INDEX: "built"}
            with scratch.begin() as conn:
                conn.execute(text("ANALYZE speaker"))
                conn.execute(text("SET LOCAL enable_seqscan = off"))
                for shape in (
                    "identity_source IN ('voiceprint', 'voiceprint_link')",
                    "identity_source = 'voiceprint'",
                    "identity_source = 'voiceprint_link'",
                ):
                    plan = "\n".join(
                        r[0]
                        for r in conn.execute(
                            text(
                                "EXPLAIN SELECT id FROM speaker "
                                f"WHERE media_file_id IN (1, 2, 3) AND {shape}"
                            )
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
                assert INDEX not in plan, "the index must not claim rows outside its predicate"


def test_the_downgrade_drops_the_index_and_is_repeatable():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _revision_module()
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database(MINIMAL_SPEAKER) as (scratch, _url):
            assert _build(scratch) == {INDEX: "built"}
            for _ in range(2):
                with (
                    scratch.begin() as conn,
                    Operations.context(MigrationContext.configure(connection=conn)),
                ):
                    module.downgrade()
                with scratch.connect() as conn:
                    assert _index_def(conn) is None


# --- full-chain scratch database: the real startup path -------------------------------------


def test_run_migrations_builds_the_index_and_survives_a_held_transaction(monkeypatch):
    """The real ``run_migrations()`` (advisory-lock connection and all) on a private database."""
    from app.db import concurrent_indexes
    from app.db import migrations
    from tests.unit._migration_detection import assert_detected_at_or_after

    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database(None) as (scratch, url):
            monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
            monkeypatch.setattr(concurrent_indexes, "BUILD_LOCK_TIMEOUT_SECONDS", 2)

            def start() -> None:
                monkeypatch.setattr(migrations, "_concurrent_index_retry_done", False)
                migrations.run_migrations()

            start()  # empty database: the whole chain, then the runner builds the index
            with scratch.connect() as conn:
                assert _valid(conn) is True
                assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)

            with _autocommit(scratch) as conn:
                conn.execute(text(f"DROP INDEX {INDEX}"))
                # Pending index: v458 runs no DDL, so a later revision's marker still places the
                # database (v459's rung does not require the index); the runner builds it below.
                assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)

            with scratch.connect().execution_options(isolation_level="REPEATABLE READ") as blocker:
                blocker.execute(text("SELECT count(*) FROM speaker"))
                started = time.monotonic()
                # Own handler: Alembic's env.py reconfigures logging, which can drop caplog's.
                records: list[logging.LogRecord] = []
                handler = _Collect(records)
                migrations.logger.addHandler(handler)
                try:
                    start()
                finally:
                    migrations.logger.removeHandler(handler)
                assert time.monotonic() - started < 120, "startup waited on the held transaction"
                blocker.rollback()
            with scratch.connect() as conn:
                # An abandoned build may leave an INVALID index (its cleanup waits on the same
                # blocker); no VALID one may have been forced through.
                assert _valid(conn) is not True
            assert any("still pending" in r.getMessage() for r in records)

            start()  # the blocker is gone: the next start drops the leftover and builds
            with scratch.connect() as conn:
                assert _valid(conn) is True
