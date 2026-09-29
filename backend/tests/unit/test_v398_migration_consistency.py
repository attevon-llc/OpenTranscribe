"""v398 migration + detection-arm consistency (``media_file.playback_path``).

Like ``v386``-``v397``, this suite **executes** the revision's SQL rather than grepping its
source, and executes it twice, because the startup runner stamps untracked databases by
schema fingerprint and therefore re-runs a revision over its own partial output.

``v398_add_media_playback_path`` adds one nullable ``VARCHAR`` to ``media_file``: the object
key of a browser-playable rendition, for originals no browser decodes (AIFF, WMA, AVI, ...).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

#: ``ddl_exclusive`` is applied PER TEST, never to the module: every EXCLUSIVE advisory
#: lock drains all other xdist workers (issue #431).

REVISION = "v398_add_media_playback_path"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_COLUMN = "playback_path"
_TABLE = "media_file"


def _revision_module():
    """Load the revision by path — ``alembic/`` is not an importable package (see v374)."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v398_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v397_add_platform_super_admin_link_authorized"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    # True while it is head, and still true once a later revision revises it.
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v398_migration_is_vendor_neutral():
    """CI's seam guard greps core for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_column_exists_and_is_nullable(db_session):
    """NULL is the meaning "play the original", so the column must accept it."""
    live = {c["name"]: c for c in inspect(db_session.connection()).get_columns(_TABLE)}
    assert _COLUMN in live, "media_file is missing playback_path"
    assert live[_COLUMN]["nullable"] is True


def test_the_orm_mirrors_the_column(db_session):
    """The half ``test_schema_drift.py`` cannot see: it compares names, not nullability."""
    from app.db.base import Base

    live = {c["name"]: c["nullable"] for c in inspect(db_session.connection()).get_columns(_TABLE)}
    model = Base.metadata.tables[_TABLE]
    assert _COLUMN in live, f"{_COLUMN} is not in the database"
    assert model.columns[_COLUMN].nullable == live[_COLUMN]


def test_detection_arm_returns_v398_or_later_on_current_schema(db_session):
    """Step 4 of the procedure in backend/app/db/CLAUDE.md — the step that gets skipped."""
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_marker(db_session):
    """Drop the marker column and the ladder must stop matching v398.

    Asserted as a *band* — at or after v397, strictly before v398 — because an exact
    ``==`` on a lower revision goes red or vacuous the next time the ladder changes.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        # finally, not a trailing call: this mutates the SHARED dev schema.
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v397_add_platform_super_admin_link_authorized")
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
        count = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'media_file' AND column_name = :col"
            ),
            {"col": _COLUMN},
        ).scalar()
        assert count == 1
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
        live = {c["name"] for c in inspect(conn).get_columns(_TABLE)}
        assert _COLUMN not in live

        conn.execute(text(module.UPGRADE_SQL))
        live = {c["name"] for c in inspect(conn).get_columns(_TABLE)}
        assert _COLUMN in live
    finally:
        db_session.rollback()
