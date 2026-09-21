"""v394 migration + detection-arm consistency (``media_file.duration_source``).

Like ``v386``-``v393``, this suite **executes** the revision's SQL rather than grepping its
source, and executes it twice, because the startup runner stamps untracked databases by
schema fingerprint and therefore re-runs a revision over its own partial output.

``v394_add_media_duration_provenance`` adds one nullable ``VARCHAR(20)`` column,
``duration_source``, plus ``ck_media_file_duration_source`` restricting it to
``container`` / ``transcript_extent`` / ``none`` (issue #969). Unlike v391's
``recorded_date_source``, there is deliberately no "value implies source" CHECK
here — every pre-existing row already has a duration and a NULL source, and this
revision does not backfill.
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

REVISION = "v394_add_media_duration_provenance"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_COLUMN = "duration_source"


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
            {"e": f"v394_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _new_file(conn, user_id: int, duration_source=None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO media_file (uuid, user_id, filename, storage_path, file_size, "
                "content_type, duration_source) "
                "VALUES (:u, :uid, 'v394.wav', 'x/v394.wav', 1, 'audio/wav', :ds) RETURNING id"
            ),
            {"u": str(uuid_pkg.uuid4()), "uid": user_id, "ds": duration_source},
        ).scalar()
    )


def test_v394_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v393_add_overlap_timing_columns"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    # True while it is head, and still true once a later revision revises it.
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v394_migration_is_vendor_neutral():
    """CI's seam guard greps core for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_column_exists_and_is_nullable_varchar(db_session):
    conn = db_session.connection()
    row = conn.execute(
        text(
            "SELECT data_type, character_maximum_length, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'media_file' AND column_name = :c"
        ),
        {"c": _COLUMN},
    ).one_or_none()
    assert row is not None, "media_file is missing duration_source"
    assert row[0] == "character varying"
    assert row[1] == 20
    assert row[2] == "YES"


def test_a_value_round_trips(db_session):
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        file_id = _new_file(conn, user_id, duration_source="container")
        assert (
            conn.execute(
                text("SELECT duration_source FROM media_file WHERE id = :f"), {"f": file_id}
            ).scalar()
            == "container"
        )
    finally:
        db_session.rollback()


def test_the_check_rejects_an_unknown_source(db_session):
    """The constraint is what keeps ``media_duration_backfill`` writing only the real vocabulary."""
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    file_id = _new_file(conn, _new_user(conn))
    with pytest.raises(IntegrityError):
        conn.execute(
            text("UPDATE media_file SET duration_source = 'bogus' WHERE id = :f"),
            {"f": file_id},
        )
    db_session.rollback()


@pytest.mark.parametrize("value", ["container", "transcript_extent", "none"])
def test_every_documented_value_can_actually_be_written(db_session, value):
    """Asserting the constraint text is not enough — v380 learned that the hard way."""
    conn = db_session.connection()
    try:
        file_id = _new_file(conn, _new_user(conn))
        conn.execute(
            text("UPDATE media_file SET duration_source = :v WHERE id = :f"),
            {"v": value, "f": file_id},
        )
        assert (
            conn.execute(
                text("SELECT duration_source FROM media_file WHERE id = :f"), {"f": file_id}
            ).scalar()
            == value
        )
    finally:
        db_session.rollback()


def test_a_row_may_carry_no_source_at_all(db_session):
    """NULL is the honest state of every row written before #969 was fixed."""
    conn = db_session.connection()
    try:
        file_id = _new_file(conn, _new_user(conn), duration_source=None)
        assert (
            conn.execute(
                text("SELECT duration_source FROM media_file WHERE id = :f"), {"f": file_id}
            ).scalar()
            is None
        )
    finally:
        db_session.rollback()


def test_the_orm_mirrors_the_column(db_session):
    """The half ``test_schema_drift.py`` cannot see: it compares names, not nullability."""
    from app.db.base import Base

    live = {
        c["name"]: c["nullable"] for c in inspect(db_session.connection()).get_columns("media_file")
    }
    model = Base.metadata.tables["media_file"]
    assert _COLUMN in live, "the column is not in the database"
    assert model.columns[_COLUMN].nullable == live[_COLUMN]
    assert live[_COLUMN] is True, "a pre-v394 row must be able to say nothing at all"


def test_detection_arm_returns_v394_or_later_on_current_schema(db_session):
    """Step 4 of the procedure in backend/app/db/CLAUDE.md — the step that gets skipped."""
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_column(db_session):
    """Drop the marker and the ladder must stop matching v394.

    Asserted as a *band* — at or after v393, strictly before v394 — because an exact
    ``==`` on a lower revision goes red or vacuous the next time the ladder changes.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f"ALTER TABLE media_file DROP COLUMN {_COLUMN}"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        # finally, not a trailing call: this mutates the SHARED dev schema.
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v393_add_overlap_timing_columns")
        <= order.index(detected)
        < order.index(REVISION)
    )


@pytest.mark.ddl_exclusive
def test_rerunning_the_upgrade_is_a_no_op(db_session):
    """The invariant the startup runner depends on, executed rather than asserted about."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'media_file' AND column_name = :c"
                ),
                {"c": _COLUMN},
            ).scalar()
            == 1
        )
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_removes_the_column_and_the_upgrade_restores_it(db_session):
    """The downgrade is executed here, not merely read."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))  # idempotent both ways
        assert _COLUMN not in {c["name"] for c in inspect(conn).get_columns("media_file")}

        conn.execute(text(module.UPGRADE_SQL))
        assert _COLUMN in {c["name"] for c in inspect(conn).get_columns("media_file")}
    finally:
        db_session.rollback()
