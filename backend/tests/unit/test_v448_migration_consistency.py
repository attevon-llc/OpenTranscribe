"""v448 migration + detection-arm consistency (one active task row per file and step, #1185)."""

from __future__ import annotations

import importlib.util
import re
import time
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.conftest import engine

REVISION = "v448_task_one_active_row_per_file_step"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_INDEX = "uq_task_active_per_file_type"
_T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_v448_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v447_add_content_hold"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


@pytest.fixture
def files(db_session, normal_user):
    """Eight media files of one user, ids returned in order."""
    conn = db_session.connection()
    ids = []
    for _ in range(8):
        ids.append(
            conn.execute(
                text(
                    "INSERT INTO media_file (filename, storage_path, file_size, content_type, "
                    "user_id, uuid) VALUES ('f', :p, 1, 'audio/wav', :u, :id) RETURNING id"
                ),
                {"p": f"v448/{uuid.uuid4().hex}", "u": normal_user.id, "id": uuid.uuid4()},
            ).scalar()
        )
    return ids


def _task(conn, user_id, file_id, task_type, status, age_days, tid=None):
    tid = tid or f"v448-{uuid.uuid4().hex[:10]}"
    conn.execute(
        text(
            "INSERT INTO task (id, user_id, media_file_id, task_type, status, created_at) "
            "VALUES (:i, :u, :f, :t, :s, :c)"
        ),
        {
            "i": tid,
            "u": user_id,
            "f": file_id,
            "t": task_type,
            "s": status,
            "c": None if age_days is None else _T0 + timedelta(days=age_days),
        },
    )
    return tid


def _row(conn, tid):
    return conn.execute(
        text("SELECT status, error_message, completed_at FROM task WHERE id = :i"), {"i": tid}
    ).one()


# --- the guard ----------------------------------------------------------------------------------


def test_the_index_refuses_a_second_active_row_for_one_file_and_step(
    db_session, normal_user, files
):
    conn = db_session.connection()
    _task(conn, normal_user.id, files[0], "summarization", "in_progress", 0)

    with pytest.raises(IntegrityError) as err:
        with conn.begin_nested():
            _task(conn, normal_user.id, files[0], "summarization", "pending", 1)

    assert _INDEX in str(err.value.orig)


def test_the_index_does_not_over_reach(db_session, normal_user, files):
    """Terminal duplicates, other steps, other files and file-less rows are all still allowed."""
    conn = db_session.connection()
    uid = normal_user.id
    _task(conn, uid, files[0], "summarization", "in_progress", 0)
    _task(conn, uid, files[0], "summarization", "completed", 1)
    _task(conn, uid, files[0], "summarization", "failed", 2)
    _task(conn, uid, files[0], "analytics", "in_progress", 3)
    _task(conn, uid, files[1], "summarization", "in_progress", 4)
    _task(conn, uid, None, "search_reembed", "in_progress", 5)
    _task(conn, uid, None, "search_reembed", "in_progress", 6)

    assert (
        conn.execute(text("SELECT count(*) FROM task WHERE user_id = :u"), {"u": uid}).scalar() == 7
    )


def test_reopening_a_finished_row_beside_an_active_one_is_refused(db_session, normal_user, files):
    conn = db_session.connection()
    old = _task(conn, normal_user.id, files[0], "summarization", "failed", 0)
    _task(conn, normal_user.id, files[0], "summarization", "in_progress", 1)

    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(text("UPDATE task SET status = 'in_progress' WHERE id = :i"), {"i": old})


# --- resolving existing violations --------------------------------------------------------------


@pytest.mark.ddl_exclusive
def test_upgrade_resolves_existing_duplicates_and_builds_the_index(db_session, normal_user, files):
    """A database already holding violating rows must upgrade, not fail on the index build."""
    conn = db_session.connection()
    uid = normal_user.id
    conn.execute(text(f"DROP INDEX {_INDEX}"))  # the state a v447 database is in

    # (a) active row older than a completed one: superseded by the completed one
    a_old = _task(conn, uid, files[0], "summarization", "in_progress", 0)
    a_done = _task(conn, uid, files[0], "summarization", "completed", 1)
    # (b) several active rows, none completed: the newest is kept
    b_old = _task(conn, uid, files[1], "analytics", "pending", 0)
    b_new = _task(conn, uid, files[1], "analytics", "in_progress", 1)
    # (a)+(b): completed in the middle, an older active superseded by it, a newer one kept
    c_old = _task(conn, uid, files[2], "topic_extraction", "in_progress", 0)
    c_done = _task(conn, uid, files[2], "topic_extraction", "completed", 1)
    c_new = _task(conn, uid, files[2], "topic_extraction", "in_progress", 2)
    # not violations: an older completed row, a different step, no file
    d_active = _task(conn, uid, files[3], "summarization", "in_progress", 2)
    d_done = _task(conn, uid, files[3], "summarization", "completed", 0)
    e_transcribe = _task(conn, uid, files[4], "transcription", "in_progress", 0)
    e_other = _task(conn, uid, files[4], "summarization", "completed", 1)
    n1 = _task(conn, uid, None, "search_reembed", "in_progress", 0)
    n2 = _task(conn, uid, None, "search_reembed", "in_progress", 1)
    # equal timestamps: the higher id is the newest
    tie_lo = _task(conn, uid, files[5], "rediarize", "in_progress", 0, tid="v448-tie-a")
    tie_hi = _task(conn, uid, files[5], "rediarize", "pending", 0, tid="v448-tie-b")
    # a row with no created_at loses to one that has it
    nul = _task(conn, uid, files[6], "analytics", "in_progress", None)
    stamped = _task(conn, uid, files[6], "analytics", "in_progress", 0)

    conn.execute(text(_module().UPGRADE_SQL))

    for old, by in [
        (a_old, a_done),
        (b_old, b_new),
        (c_old, c_done),
        (tie_lo, tie_hi),
        (nul, stamped),
    ]:
        status, message, completed_at = _row(conn, old)
        assert (status, message) == ("failed", f"superseded by {by}"), old
        assert completed_at is not None
    for kept in [a_done, c_done, d_done, e_other]:
        assert _row(conn, kept)[0] == "completed"
    for kept in [b_new, c_new, d_active, e_transcribe, n1, n2, tie_hi, stamped]:
        status, message, _ = _row(conn, kept)
        assert status in ("in_progress", "pending") and message is None, kept

    # the guard is back, and no further violation exists for it to refuse
    assert conn.execute(
        text("SELECT 1 FROM pg_indexes WHERE indexname = :n"), {"n": _INDEX}
    ).scalar()
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            _task(conn, uid, files[1], "analytics", "pending", 5)


@pytest.mark.ddl_exclusive
def test_upgrade_is_idempotent(db_session, normal_user, files):
    conn = db_session.connection()
    conn.execute(text(f"DROP INDEX {_INDEX}"))  # a database that is still at v447
    _task(conn, normal_user.id, files[0], "summarization", "in_progress", 0)
    sql = _module().UPGRADE_SQL

    conn.execute(text(sql))
    snapshot = conn.execute(text("SELECT id, status, error_message FROM task ORDER BY id")).all()
    conn.execute(text(sql))

    assert (
        conn.execute(text("SELECT id, status, error_message FROM task ORDER BY id")).all()
        == snapshot
    )
    assert (
        conn.execute(
            text("SELECT count(*) FROM pg_indexes WHERE indexname = :n"), {"n": _INDEX}
        ).scalar()
        == 1
    )


def test_downgrade_drops_only_this_revisions_index():
    assert re.findall(r"DROP INDEX IF EXISTS (\w+)", _module().DOWNGRADE_SQL) == [_INDEX]
    assert "DROP TABLE" not in _module().DOWNGRADE_SQL.upper()


@pytest.mark.ddl_exclusive
def test_downgrade_then_upgrade_restores_the_same_shape(db_session, normal_user, files):
    conn = db_session.connection()
    uid = normal_user.id
    shape = text("SELECT indexdef FROM pg_indexes WHERE indexname = :n")
    before = conn.execute(shape, {"n": _INDEX}).scalar()
    assert before is not None

    conn.execute(text(_module().DOWNGRADE_SQL))
    assert conn.execute(shape, {"n": _INDEX}).scalar() is None
    # without the guard two active rows coexist, which is exactly what the upgrade must resolve
    older = _task(conn, uid, files[0], "analytics", "in_progress", 0)
    newer = _task(conn, uid, files[0], "analytics", "in_progress", 1)
    conn.execute(text(_module().UPGRADE_SQL))

    assert conn.execute(shape, {"n": _INDEX}).scalar() == before
    assert _row(conn, older)[:2] == ("failed", f"superseded by {newer}")


# --- detection arm ------------------------------------------------------------------------------


def test_detection_arm_returns_v448_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_index(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f"DROP INDEX {_INDEX}"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)


# --- the table lock is bounded ------------------------------------------------------------------


def test_the_lock_is_taken_under_a_lock_timeout_and_the_callers_value_is_restored():
    sql = _module().UPGRADE_SQL
    lock = sql.index("LOCK TABLE task IN SHARE ROW EXCLUSIVE MODE")
    assert sql.index("set_config('lock_timeout', '30s', true)") < lock
    assert sql.index("set_config('lock_timeout', previous, true)") > lock
    assert _module().LOCK_TIMEOUT == "30s"


@pytest.mark.ddl_exclusive
def test_the_upgrade_does_not_change_the_sessions_lock_timeout(db_session):
    conn = db_session.connection()
    conn.execute(text("SET LOCAL lock_timeout = '7s'"))

    conn.execute(text(_module().UPGRADE_SQL))

    assert conn.execute(text("SHOW lock_timeout")).scalar() == "7s"


@pytest.mark.ddl_exclusive
def test_a_conflicting_lock_fails_the_upgrade_fast_with_a_message_naming_the_cause(db_session):
    """Without a timeout the LOCK queues, and every writer to task queues behind it."""
    from sqlalchemy.exc import DBAPIError

    holder = engine.connect()
    try:
        holder_tx = holder.begin()
        holder.execute(text("LOCK TABLE task IN ROW EXCLUSIVE MODE"))
        conn = db_session.connection()
        started = time.monotonic()

        with pytest.raises(DBAPIError) as err:
            with conn.begin_nested():
                conn.execute(text(_module().build_upgrade_sql("300ms")))

        assert time.monotonic() - started < 10
        message = str(err.value.orig)
        assert "v448 could not lock the task table within 300ms" in message
        assert "safe to retry" in message
        holder_tx.rollback()
    finally:
        holder.close()


@pytest.mark.ddl_exclusive
def test_the_same_upgrade_succeeds_when_nothing_holds_the_table(db_session):
    """Control for the failure above: only the competing lock differs."""
    conn = db_session.connection()
    conn.execute(text(_module().build_upgrade_sql("300ms")))
    assert conn.execute(
        text("SELECT 1 FROM pg_indexes WHERE indexname = :n"), {"n": _INDEX}
    ).scalar()
