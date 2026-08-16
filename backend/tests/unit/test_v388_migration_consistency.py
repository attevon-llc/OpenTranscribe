"""v388 migration + detection-arm consistency (``user_group.organization_id``).

``user_group`` was the only user-owned table without an ``organization_id`` stamp, so the
group plane had no column a tenant filter could read — see
``tests/api/test_groups_tenancy.py`` for the behaviour that made that exploitable.

Like ``v386``/``v387``, this suite **executes** the revision's SQL rather than grepping its
source, and executes it twice, because the startup runner stamps untracked databases by
schema fingerprint and therefore re-runs a revision over its own partial output. A
migration failure is ``SystemExit(1)``, so a non-idempotent revision does not degrade — the
backend refuses to start.

The backfill gets its own tests because it is the part with a *judgement* in it: a group is
stamped only when its owner's tenancy is unambiguous, and "unambiguous" is a rule that can
be got wrong silently in either direction.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

#: ``ddl_exclusive`` is applied PER TEST, never to the module: every EXCLUSIVE advisory lock
#: drains all other xdist workers, so a read-only schema assertion carrying it costs a
#: full-suite barrier for nothing (issue #431). ``tests/unit/test_ddl_marker_discipline.py``
#: enforces both directions.

REVISION = "v388_add_user_group_organization_id"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    """Load the revision by path — ``alembic/`` is not an importable package (see v374)."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_user(conn, email_prefix: str = "v388") -> int:
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
            ),
            {"e": f"{email_prefix}_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _new_org(conn) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO organization (uuid, name, is_active) VALUES (:u, :n, true) "
                "RETURNING id"
            ),
            {"u": str(uuid_pkg.uuid4()), "n": f"v388-{uuid_pkg.uuid4().hex[:8]}"},
        ).scalar()
    )


def _new_group(conn, owner_id: int, *, organization_id: int | None = None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO user_group (uuid, name, owner_id, organization_id) "
                "VALUES (:u, :n, :o, :org) RETURNING id"
            ),
            {
                "u": str(uuid_pkg.uuid4()),
                "n": f"v388-{uuid_pkg.uuid4().hex[:10]}",
                "o": owner_id,
                "org": organization_id,
            },
        ).scalar()
    )


def _join(conn, org_id: int, user_id: int) -> None:
    conn.execute(
        text(
            "INSERT INTO organization_membership (organization_id, user_id, role) "
            "VALUES (:o, :u, 'org:member')"
        ),
        {"o": org_id, "u": user_id},
    )


def test_v388_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v387_actor_fks_and_tag_share_check"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    # True while it is head, and still true once v389 revises it.
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v388_migration_is_vendor_neutral():
    """CI's seam guard greps core for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_column_index_and_fk_all_exist(db_session):
    """All three objects, not just the column.

    A column without its FK is an untyped integer that can name a deleted tenant, and
    without the index every tenant-filtered group query is a sequential scan. Asserted
    together because the revision adds them together and a partial hand-repair is the
    realistic failure.
    """
    conn = db_session.connection()
    columns = {c["name"]: c for c in inspect(conn).get_columns("user_group")}
    assert "organization_id" in columns
    assert columns["organization_id"]["nullable"] is True, (
        "NULL is personal scope — a NOT NULL stamp would make the community edition "
        "impossible, since it has no organizations at all"
    )

    assert conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'user_group_organization_id_fkey')"
        )
    ).scalar()
    assert conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes "
            "WHERE indexname = 'ix_user_group_organization_id')"
        )
    ).scalar()


def test_the_fk_has_no_on_delete_rule(db_session):
    """``NO ACTION``, matching the other 11 FKs into ``organization``.

    A cascade would delete groups with their tenant; a SET NULL would silently convert an
    org's groups into their owners' *personal* groups — re-exposing tenant data as personal
    data, which is worse than the error. Whole-tenant erasure is an explicit operation
    (``POST /org-admin/gdpr/erase-organization``), never a side effect.
    """
    rule = (
        db_session.connection()
        .execute(
            text(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conname = 'user_group_organization_id_fkey'"
            )
        )
        .scalar()
    )
    assert rule == "a", f"expected NO ACTION ('a'), found {rule!r}"


def test_the_orm_mirrors_the_column(db_session):
    """The half ``test_schema_drift.py`` cannot see: it compares columns, not nullability.

    ``AuthConfigAudit.changed_by`` shipped as ``nullable=False`` against a nullable column
    for exactly this reason (v387), so the check is repeated here for the new column.
    """
    from app.db.base import Base

    live = {
        c["name"]: c["nullable"] for c in inspect(db_session.connection()).get_columns("user_group")
    }
    model_column = Base.metadata.tables["user_group"].columns["organization_id"]

    assert model_column.nullable == live["organization_id"]
    assert [fk.target_fullname for fk in model_column.foreign_keys] == ["organization.id"]


def test_detection_arm_returns_v388_or_later_on_current_schema(db_session):
    """Step 4 of the procedure in backend/app/db/CLAUDE.md — the step that gets skipped.

    Skip the arm and an untracked database is mis-stamped to the PREVIOUS revision and
    never receives this DDL, so the tenant filter reads a column that is not there.
    """
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_column(db_session):
    """Drop the marker and the ladder must stop matching v388.

    Asserted as a *band* — at or after v387, strictly before v388 — because an exact ``==``
    on a lower revision goes red or vacuous the next time the ladder above it changes, which
    had already happened to three suites in this family.

    Verified still correct after **v389** (`file_facts`) landed: v389's arm is cumulative,
    so it requires ``user_group.organization_id`` too and fails alongside v388's when the
    column is dropped. The band is what makes that a no-op here instead of the usual
    "each new migration breaks its predecessor's detection test" (backend/tests/CLAUDE.md).
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("ALTER TABLE user_group DROP COLUMN organization_id"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        # finally, not a trailing call: this mutates the SHARED dev schema, so anything
        # raising in between must still undo it.
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v387_actor_fks_and_tag_share_check")
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

        columns = {c["name"] for c in inspect(conn).get_columns("user_group")}
        assert "organization_id" in columns
        # Exactly one FK and one index survive two runs — the guards are guards, not
        # duplicate-object generators.
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'user_group_organization_id_fkey'"
                )
            ).scalar()
            == 1
        )
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_indexes "
                    "WHERE indexname = 'ix_user_group_organization_id'"
                )
            ).scalar()
            == 1
        )
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_removes_all_three_objects_and_the_upgrade_restores_them(db_session):
    """The downgrade is executed here, not merely read.

    Before issue #431 no downgrade in this chain had ever been run by a test, so
    "``downgrade()`` mirrors ``upgrade()``" was a claim about source text.
    """
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))  # idempotent both ways

        assert "organization_id" not in {c["name"] for c in inspect(conn).get_columns("user_group")}
        assert not conn.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM pg_indexes "
                "WHERE indexname = 'ix_user_group_organization_id')"
            )
        ).scalar()

        conn.execute(text(module.UPGRADE_SQL))
        assert "organization_id" in {c["name"] for c in inspect(conn).get_columns("user_group")}
    finally:
        db_session.rollback()


# ------------------------------------------------------------------- backfill


def test_the_backfill_stamps_a_group_whose_owner_is_in_exactly_one_org(db_session):
    conn = db_session.connection()
    try:
        owner = _new_user(conn)
        org = _new_org(conn)
        _join(conn, org, owner)
        group = _new_group(conn, owner, organization_id=None)

        conn.execute(text(_revision_module().BACKFILL_SQL))

        stamped = conn.execute(
            text("SELECT organization_id FROM user_group WHERE id = :g"), {"g": group}
        ).scalar()
        assert stamped == org
    finally:
        db_session.rollback()


def test_the_backfill_leaves_a_multi_org_owners_group_alone(db_session):
    """Two memberships means there is no answer to derive, so it must not guess.

    A wrong stamp is worse than none: it silently files the group under one tenant and hides
    it from the other, and nothing downstream can tell that it was a guess.
    """
    conn = db_session.connection()
    try:
        owner = _new_user(conn)
        _join(conn, _new_org(conn), owner)
        _join(conn, _new_org(conn), owner)
        group = _new_group(conn, owner, organization_id=None)

        conn.execute(text(_revision_module().BACKFILL_SQL))

        assert (
            conn.execute(
                text("SELECT organization_id FROM user_group WHERE id = :g"), {"g": group}
            ).scalar()
            is None
        )
    finally:
        db_session.rollback()


def test_the_backfill_leaves_a_community_owners_group_alone(db_session):
    """Zero memberships is every community-edition account — must stay personal scope."""
    conn = db_session.connection()
    try:
        group = _new_group(conn, _new_user(conn), organization_id=None)

        conn.execute(text(_revision_module().BACKFILL_SQL))

        assert (
            conn.execute(
                text("SELECT organization_id FROM user_group WHERE id = :g"), {"g": group}
            ).scalar()
            is None
        )
    finally:
        db_session.rollback()


def test_the_backfill_never_overwrites_an_existing_stamp(db_session):
    """Re-running must not re-file a group that has already been scoped by a human.

    The ``WHERE organization_id IS NULL`` guard is what makes the revision re-runnable
    without changing its own previous answer.
    """
    conn = db_session.connection()
    try:
        owner = _new_user(conn)
        already, other = _new_org(conn), _new_org(conn)
        _join(conn, other, owner)  # the backfill would derive `other`
        group = _new_group(conn, owner, organization_id=already)

        conn.execute(text(_revision_module().BACKFILL_SQL))

        assert (
            conn.execute(
                text("SELECT organization_id FROM user_group WHERE id = :g"), {"g": group}
            ).scalar()
            == already
        )
    finally:
        db_session.rollback()
