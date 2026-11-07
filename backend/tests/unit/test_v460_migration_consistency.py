"""v460 migration + detection-arm consistency (LLM token-limit counters, issue #790).

Executes the revision's SQL (twice, because the startup runner re-runs a revision over its own
partial output when it stamps an untracked database) rather than grepping its source.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

REVISION = "v460_add_llm_token_budget"
TABLE = "llm_token_budget"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v460_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)

    assert rev.down_revision == "v458_speaker_machine_identity_index"
    assert len(set(scripts.get_heads())) == 1, "two heads mean two branches claimed a number"


def test_the_orm_mirrors_the_table_and_a_user_takes_its_counters_with_it(db_session):
    from app.db.base import Base

    conn = db_session.connection()
    live = {c["name"] for c in inspect(conn).get_columns(TABLE)}
    assert live == set(Base.metadata.tables[TABLE].columns.keys())
    rule = conn.execute(
        text(
            "SELECT confdeltype FROM pg_constraint "
            "WHERE conrelid = CAST(:t AS regclass) AND contype = 'f'"
        ),
        {"t": TABLE},
    ).scalar()
    assert rule == "c"


@pytest.mark.parametrize(
    ("scope", "user", "used", "constraint"),
    [
        ("forever", None, 0, "ck_llm_token_budget_scope"),
        ("user_day", None, 0, "ck_llm_token_budget_subject"),
        ("instance_month", "user", 0, "ck_llm_token_budget_subject"),
        ("instance_month", None, -1, "ck_llm_token_budget_used"),
    ],
)
def test_the_checks_refuse_an_impossible_counter(
    db_session, normal_user, scope, user, used, constraint
):
    conn = db_session.connection()
    with pytest.raises(IntegrityError, match=constraint):
        with conn.begin_nested():
            conn.execute(
                text(
                    "INSERT INTO llm_token_budget (scope, user_id, period, used) "
                    "VALUES (:s, :u, '2040-01', :n)"
                ),
                {"s": scope, "u": normal_user.id if user else None, "n": used},
            )
    db_session.rollback()


def test_one_counter_per_scope_subject_and_period(db_session):
    conn = db_session.connection()
    insert = text(
        "INSERT INTO llm_token_budget (scope, user_id, period, used) "
        "VALUES ('instance_month', NULL, '2041-02', 0)"
    )
    conn.execute(insert)
    with pytest.raises(IntegrityError, match="uq_llm_token_budget_counter"):
        with conn.begin_nested():
            conn.execute(insert)
    db_session.rollback()


@pytest.mark.ddl_exclusive
def test_downgrade_and_upgrade_are_idempotent(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))
        assert TABLE not in inspect(conn).get_table_names()

        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))
        assert TABLE in inspect(conn).get_table_names()
    finally:
        db_session.rollback()


#: v458's index is built by the app runner after the upgrade, not by alembic, so a database
#: migrated by ``alembic upgrade head`` alone (this suite's) lacks it until the app starts, and
#: v460's rung sits above v458's. Built inside each test's rolled-back transaction.
_V458_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_speaker_machine_identity_media_file ON speaker "
    "(media_file_id) WHERE identity_source IN ('voiceprint', 'voiceprint_link')"
)


def test_the_v458_index_here_is_the_one_the_runner_builds():
    from app.db.speaker_indexes import MACHINE_IDENTITY

    assert MACHINE_IDENTITY.name in _V458_INDEX_SQL
    assert MACHINE_IDENTITY.where is not None and MACHINE_IDENTITY.where in _V458_INDEX_SQL


@pytest.mark.ddl_exclusive
def test_detection_arm_returns_v460_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    try:
        conn.execute(text(_V458_INDEX_SQL))
        assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_marker_index(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(_V458_INDEX_SQL))
        conn.execute(text("DROP INDEX uq_llm_token_budget_counter"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()

    assert detected is not None, "the ladder no longer recognizes the pre-v460 schema"
    order = _chain_order()
    assert order.index(detected) < order.index(REVISION)
