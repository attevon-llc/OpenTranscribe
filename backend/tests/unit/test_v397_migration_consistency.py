"""v397 migration + detection-arm consistency (``user.platform_super_admin_link_authorized``).

Like ``v386``-``v393``, this suite **executes** the revision's SQL rather than grepping its
source, and executes it twice, because the startup runner stamps untracked databases by
schema fingerprint and therefore re-runs a revision over its own partial output.

``v397_add_platform_super_admin_link_authorized`` adds a single ``BOOLEAN NOT NULL DEFAULT
FALSE`` column to ``user``: the escape hatch ``auth.account_linking.
assert_provider_id_link_permitted``'s rule 1 reads to decide whether to skip its otherwise-
unconditional refusal to JIT-link/refresh an external identity onto a ``role == super_admin``
row (issue #993). See that revision file's docstring for why it is numbered v397 rather than
the next number after v393.
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

REVISION = "v397_add_platform_super_admin_link_authorized"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_COLUMN = "platform_super_admin_link_authorized"


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
            {"e": f"v397_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def test_v397_revision_chain():
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


def test_v397_migration_is_vendor_neutral():
    """CI's seam guard greps core for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_column_exists_and_is_not_null_boolean(db_session):
    conn = db_session.connection()
    live = {c["name"]: c for c in inspect(conn).get_columns("user")}
    assert _COLUMN in live, "user is missing platform_super_admin_link_authorized"
    assert live[_COLUMN]["nullable"] is False


def test_the_column_defaults_false_for_a_new_row(db_session):
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        value = conn.execute(
            text(f'SELECT {_COLUMN} FROM "user" WHERE id = :id'), {"id": user_id}
        ).scalar()
        assert value is False
    finally:
        db_session.rollback()


def test_the_value_round_trips(db_session):
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        conn.execute(text(f'UPDATE "user" SET {_COLUMN} = true WHERE id = :id'), {"id": user_id})
        value = conn.execute(
            text(f'SELECT {_COLUMN} FROM "user" WHERE id = :id'), {"id": user_id}
        ).scalar()
        assert value is True
    finally:
        db_session.rollback()


def test_the_orm_mirrors_the_column(db_session):
    """The half ``test_schema_drift.py`` cannot see: it compares names, not nullability."""
    from app.db.base import Base

    live = {c["name"]: c["nullable"] for c in inspect(db_session.connection()).get_columns("user")}
    model = Base.metadata.tables["user"]
    assert _COLUMN in live, f"{_COLUMN} is not in the database"
    assert model.columns[_COLUMN].nullable == live[_COLUMN]


def test_detection_arm_returns_v397_or_later_on_current_schema(db_session):
    """Step 4 of the procedure in backend/app/db/CLAUDE.md — the step that gets skipped."""
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_marker(db_session):
    """Drop the marker column and the ladder must stop matching v397.

    Asserted as a *band* — at or after v393, strictly before v397 — because an exact
    ``==`` on a lower revision goes red or vacuous the next time the ladder changes.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f'ALTER TABLE "user" DROP COLUMN {_COLUMN}'))
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
        count = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'user' AND column_name = :col"
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
        live = {c["name"] for c in inspect(conn).get_columns("user")}
        assert _COLUMN not in live

        conn.execute(text(module.UPGRADE_SQL))
        live = {c["name"] for c in inspect(conn).get_columns("user")}
        assert _COLUMN in live
    finally:
        db_session.rollback()
