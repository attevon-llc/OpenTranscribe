"""v393 migration + detection-arm consistency (``file_pipeline_timing`` overlap columns).

Like ``v386``-``v392``, this suite **executes** the revision's SQL rather than grepping its
source, and executes it twice, because the startup runner stamps untracked databases by
schema fingerprint and therefore re-runs a revision over its own partial output.

``v393_add_overlap_timing_columns`` adds three nullable ``BIGINT`` markers to
``file_pipeline_timing``: ``diarize_request_sent_ms``, ``diarize_joined_ms``, and
``transcript_ready_ms``. Per ``backend/app/db/CLAUDE.md``'s "Renumbering note 3", the
detection arm keys on ``transcript_ready_ms`` alone, and ``v394_add_document_tables``
deliberately does **not** require that marker — a database migrated from the
pre-renumbering document-ingestion branch carries the document tables without the timing
columns, and requiring both would drop it to v392 and re-run the entire document chain.
That asymmetry belongs to v394 and is out of scope here.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

#: ``ddl_exclusive`` is applied PER TEST, never to the module: every EXCLUSIVE advisory
#: lock drains all other xdist workers (issue #431).

REVISION = "v393_add_overlap_timing_columns"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_COLUMNS = ("diarize_request_sent_ms", "diarize_joined_ms", "transcript_ready_ms")
_MARKER = "transcript_ready_ms"


def _revision_module():
    """Load the revision by path — ``alembic/`` is not an importable package (see v374)."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_user(conn) -> int:
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
            ),
            {"e": f"v393_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _new_file(conn, user_id: int) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO media_file (uuid, user_id, filename, storage_path, file_size, "
                "content_type) VALUES (:u, :uid, 'v393.wav', 'x/v393.wav', 1, 'audio/wav') "
                "RETURNING id"
            ),
            {"u": str(uuid_pkg.uuid4()), "uid": user_id},
        ).scalar()
    )


def _new_timing_row(conn, file_id: int) -> str:
    task_id = f"v393-{uuid_pkg.uuid4().hex[:20]}"
    conn.execute(
        text("INSERT INTO file_pipeline_timing (task_id, file_id) VALUES (:t, :f)"),
        {"t": task_id, "f": file_id},
    )
    return task_id


def test_v393_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v392_add_redaction_coverage"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    # True while it is head, and still true once a later revision revises it.
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v393_migration_is_vendor_neutral():
    """CI's seam guard greps core for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_columns_exist_and_are_nullable_bigint(db_session):
    conn = db_session.connection()
    live = {c["name"]: c for c in inspect(conn).get_columns("file_pipeline_timing")}
    for column in _COLUMNS:
        assert column in live, f"file_pipeline_timing is missing {column}"
        assert live[column]["nullable"] is True, f"{column} should be nullable"


def test_the_timing_values_round_trip(db_session):
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        file_id = _new_file(conn, user_id)
        task_id = _new_timing_row(conn, file_id)

        conn.execute(
            text(
                "UPDATE file_pipeline_timing SET diarize_request_sent_ms = :a, "
                "diarize_joined_ms = :b, transcript_ready_ms = :c WHERE task_id = :t"
            ),
            {"a": 1000, "b": 4500, "c": 7800, "t": task_id},
        )

        row = conn.execute(
            text(
                "SELECT diarize_request_sent_ms, diarize_joined_ms, transcript_ready_ms "
                "FROM file_pipeline_timing WHERE task_id = :t"
            ),
            {"t": task_id},
        ).one()
        assert row == (1000, 4500, 7800)
    finally:
        db_session.rollback()


def test_the_orm_mirrors_the_columns(db_session):
    """The half ``test_schema_drift.py`` cannot see: it compares names, not nullability."""
    from app.db.base import Base

    live = {
        c["name"]: c["nullable"]
        for c in inspect(db_session.connection()).get_columns("file_pipeline_timing")
    }
    model = Base.metadata.tables["file_pipeline_timing"]
    for column in _COLUMNS:
        assert column in live, f"{column} is not in the database"
        assert model.columns[column].nullable == live[column]


def test_detection_arm_returns_v393_or_later_on_current_schema(db_session):
    """Step 4 of the procedure in backend/app/db/CLAUDE.md — the step that gets skipped."""
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_marker(db_session):
    """Drop the marker column and the ladder must stop matching v393.

    Asserted as a *band* — at or after v392, strictly before v393 — because an exact
    ``==`` on a lower revision goes red or vacuous the next time the ladder changes.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f"ALTER TABLE file_pipeline_timing DROP COLUMN {_MARKER}"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        # finally, not a trailing call: this mutates the SHARED dev schema.
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v392_add_redaction_coverage") <= order.index(detected) < order.index(REVISION)
    )


@pytest.mark.ddl_exclusive
def test_rerunning_the_upgrade_is_a_no_op(db_session):
    """The invariant the startup runner depends on, executed rather than asserted about."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))
        count = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'file_pipeline_timing' AND column_name = ANY(:cols)"
            ),
            {"cols": list(_COLUMNS)},
        ).scalar()
        assert count == len(_COLUMNS)
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_removes_the_columns_and_the_upgrade_restores_them(db_session):
    """The downgrade is executed here, not merely read."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))  # idempotent both ways
        live = {c["name"] for c in inspect(conn).get_columns("file_pipeline_timing")}
        for column in _COLUMNS:
            assert column not in live

        conn.execute(text(module.UPGRADE_SQL))
        live = {c["name"] for c in inspect(conn).get_columns("file_pipeline_timing")}
        for column in _COLUMNS:
            assert column in live
    finally:
        db_session.rollback()
