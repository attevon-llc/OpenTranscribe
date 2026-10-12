"""v432 migration + detection-arm consistency (``support_access_grant`` / ``support_access_use``).

Like ``v386``-``v431``, this suite **executes** the revision's SQL rather than grepping its
source, and executes it twice, because the startup runner stamps untracked databases by
schema fingerprint and therefore re-runs a revision over its own partial output.

``v432_support_access_grant`` (issue #1122) adds the two tables behind time-boxed platform
access to a tenant. Every foreign key on the grant is ``SET NULL`` (the grant is evidence and
must outlive the account or tenant it names); the use log carries NO foreign key to a tenant
or an owner, only ids, so it survives their erasure.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

#: ``ddl_exclusive`` is applied PER TEST, never to the module: every EXCLUSIVE advisory
#: lock drains all other xdist workers (issue #431).

REVISION = "v432_support_access_grant"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_MARKER = "ck_support_access_grant_target_kind"


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
            {"e": f"v432_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _new_org(conn) -> int:
    return int(
        conn.execute(
            text("INSERT INTO organization (uuid, name) VALUES (:u, :n) RETURNING id"),
            {"u": str(uuid_pkg.uuid4()), "n": f"v432-{uuid_pkg.uuid4().hex[:10]}"},
        ).scalar()
    )


def _insert_grant(conn, **overrides) -> int:
    values = {
        "uuid": str(uuid_pkg.uuid4()),
        "target_kind": "organization",
        "organization_id": None,
        "subject_user_id": None,
        "grantee_user_id": None,
        "access_level": "read",
        "grant_mode": "approved",
        "reason": "Investigating a customer-reported problem",
        "ticket_ref": None,
        "duration": 60,
        **overrides,
    }
    return int(
        conn.execute(
            text(
                "INSERT INTO support_access_grant (uuid, target_kind, organization_id, "
                "subject_user_id, grantee_user_id, access_level, grant_mode, reason, "
                "ticket_ref, requested_duration_minutes) VALUES (:uuid, :target_kind, "
                ":organization_id, :subject_user_id, :grantee_user_id, :access_level, "
                ":grant_mode, :reason, :ticket_ref, :duration) RETURNING id"
            ),
            values,
        ).scalar()
    )


def test_v432_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v431_add_media_duration_provenance"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    # True while it is head, and still true once a later revision revises it.
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v432_migration_is_vendor_neutral():
    """CI's seam guard greps core for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_marker_check_is_created_last():
    """A database interrupted part-way must not look finished to the detection arm."""
    module = _revision_module()
    assert module.CHECKS[-1][0] == _MARKER


def test_both_tables_exist_with_their_columns(db_session):
    inspector = inspect(db_session.connection())
    grant = {c["name"]: c for c in inspector.get_columns("support_access_grant")}
    use = {c["name"]: c for c in inspector.get_columns("support_access_use")}
    assert {
        "uuid",
        "target_kind",
        "organization_id",
        "subject_user_id",
        "grantee_user_id",
        "access_level",
        "grant_mode",
        "reason",
        "ticket_ref",
        "requested_duration_minutes",
        "requested_at",
        "decided_by_user_id",
        "decided_at",
        "decision",
        "starts_at",
        "expires_at",
        "revoked_at",
        "revoked_by_user_id",
    } <= set(grant)
    assert {
        "grant_id",
        "occurred_at",
        "method",
        "route",
        "resource_type",
        "resource_uuid",
        "need",
        "organization_id",
        "owner_user_id",
    } <= set(use)
    assert grant["reason"]["nullable"] is False
    assert use["grant_id"]["nullable"] is False


def test_every_grant_foreign_key_is_set_null(db_session):
    """Cascading would delete the evidence exactly when the tenant or account is erased."""
    rows = (
        db_session.connection()
        .execute(
            text(
                "SELECT a.attname, c.confdeltype FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
                "WHERE c.contype = 'f' AND c.conrelid = 'support_access_grant'::regclass"
            )
        )
        .all()
    )
    by_column = {name: rule for name, rule in rows}
    assert set(by_column) == {
        "organization_id",
        "subject_user_id",
        "grantee_user_id",
        "decided_by_user_id",
        "revoked_by_user_id",
    }
    assert set(by_column.values()) == {"n"}, by_column


def test_the_use_log_has_no_foreign_key_to_a_tenant_or_an_owner(db_session):
    """Snapshot stamps (the erasure_ledger pattern): they must outlive what they name."""
    rows = (
        db_session.connection()
        .execute(
            text(
                "SELECT a.attname FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
                "WHERE c.contype = 'f' AND c.conrelid = 'support_access_use'::regclass"
            )
        )
        .all()
    )
    assert {r[0] for r in rows} == {"grant_id"}


def test_deleting_the_grantee_keeps_the_grant(db_session):
    conn = db_session.connection()
    try:
        grantee = _new_user(conn)
        grant_id = _insert_grant(conn, organization_id=_new_org(conn), grantee_user_id=grantee)
        conn.execute(text('DELETE FROM "user" WHERE id = :u'), {"u": grantee})
        assert (
            conn.execute(
                text("SELECT grantee_user_id FROM support_access_grant WHERE id = :g"),
                {"g": grant_id},
            ).one()[0]
            is None
        )
    finally:
        db_session.rollback()


def test_deleting_the_organization_keeps_the_grant(db_session):
    conn = db_session.connection()
    try:
        org_id = _new_org(conn)
        grant_id = _insert_grant(conn, organization_id=org_id)
        conn.execute(text("DELETE FROM organization WHERE id = :o"), {"o": org_id})
        assert (
            conn.execute(
                text("SELECT organization_id FROM support_access_grant WHERE id = :g"),
                {"g": grant_id},
            ).one()[0]
            is None
        )
    finally:
        db_session.rollback()


def test_a_grant_with_uses_cannot_be_deleted(db_session):
    """RESTRICT: the use log is never silently orphaned or cascaded away."""
    conn = db_session.connection()
    try:
        grant_id = _insert_grant(conn, organization_id=_new_org(conn))
        conn.execute(
            text(
                "INSERT INTO support_access_use (grant_id, method, route) VALUES (:g, 'GET', '/x')"
            ),
            {"g": grant_id},
        )
        with pytest.raises(IntegrityError):
            conn.execute(text("DELETE FROM support_access_grant WHERE id = :g"), {"g": grant_id})
    finally:
        db_session.rollback()


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"target_kind": "tenant"}, "unknown target kind"),
        ({"access_level": "admin"}, "unknown access level"),
        ({"grant_mode": "emergency"}, "unknown grant mode"),
        ({"duration": 5}, "duration under 15 minutes"),
        ({"duration": 600}, "duration over 8 hours"),
        ({"reason": "too short"}, "reason under 10 characters"),
        ({"grant_mode": "break_glass", "duration": 60}, "break glass without a ticket"),
        (
            {"grant_mode": "break_glass", "ticket_ref": "INC-1", "duration": 300},
            "break glass over 4 hours",
        ),
        ({"target_kind": "personal", "organization_id": -1}, "personal target with an org"),
    ],
)
def test_the_checks_reject_what_they_name(db_session, overrides, why):
    """Each CHECK rejects the value it exists for (a constraint's text proves nothing)."""
    conn = db_session.connection()
    if "organization_id" in overrides and overrides["organization_id"] == -1:
        overrides = {**overrides, "organization_id": _new_org(conn)}
    with pytest.raises(IntegrityError):
        _insert_grant(conn, **overrides)
    db_session.rollback()


def test_an_organization_target_cannot_also_name_a_subject(db_session):
    conn = db_session.connection()
    org_id, subject = _new_org(conn), _new_user(conn)
    with pytest.raises(IntegrityError):
        _insert_grant(conn, organization_id=org_id, subject_user_id=subject)
    db_session.rollback()


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_kind": "organization"},
        {"target_kind": "personal"},
        {"access_level": "write"},
        {"grant_mode": "break_glass", "ticket_ref": "INC-9", "duration": 240},
    ],
)
def test_every_documented_value_can_actually_be_written(db_session, overrides):
    """Asserting the constraint text is not enough — v380 learned that the hard way."""
    conn = db_session.connection()
    try:
        values = dict(overrides)
        if values.get("target_kind") == "personal":
            values["subject_user_id"] = _new_user(conn)
        else:
            values["organization_id"] = _new_org(conn)
        assert _insert_grant(conn, **values) > 0
    finally:
        db_session.rollback()


def test_the_orm_mirrors_the_tables(db_session):
    """The half ``test_schema_drift.py`` cannot see: it compares names, not nullability."""
    from app.db.base import Base

    inspector = inspect(db_session.connection())
    for table in ("support_access_grant", "support_access_use"):
        live = {c["name"]: c["nullable"] for c in inspector.get_columns(table)}
        model = Base.metadata.tables[table]
        assert set(model.columns.keys()) == set(live), table
        for column in model.columns:
            assert column.nullable == live[column.name], f"{table}.{column.name}"


def test_detection_arm_returns_v432_or_later_on_current_schema(db_session):
    """Step 4 of the procedure in backend/app/db/CLAUDE.md — the step that gets skipped."""
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_marker(db_session):
    """Drop the marker CHECK and the ladder must stop matching v432.

    Asserted as a *band* — at or after v431, strictly before v432 — because an exact
    ``==`` on a lower revision goes red or vacuous the next time the ladder changes.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f"ALTER TABLE support_access_grant DROP CONSTRAINT {_MARKER}"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        # finally, not a trailing call: this mutates the SHARED dev schema.
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v431_add_media_duration_provenance")
        <= order.index(detected)
        < order.index(REVISION)
    )


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_tables(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("DROP TABLE support_access_use"))
        conn.execute(text("DROP TABLE support_access_grant"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()

    assert detected is not None
    order = _chain_order()
    assert order.index(detected) < order.index(REVISION)


def test_rerunning_the_upgrade_is_a_no_op(db_session):
    """The invariant the startup runner depends on, executed rather than asserted about."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))
        for _name, table, _body in module.CHECKS:
            assert table in inspect(conn).get_table_names()
        count = conn.execute(
            text("SELECT count(*) FROM pg_constraint WHERE conname = :c"), {"c": _MARKER}
        ).scalar()
        assert count == 1
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_a_partial_earlier_run_is_completed(db_session):
    """Tables present but the later CHECKs missing: the re-run must finish the job."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(f"ALTER TABLE support_access_grant DROP CONSTRAINT {_MARKER}"))
        conn.execute(
            text("ALTER TABLE support_access_grant DROP CONSTRAINT ck_support_access_grant_window")
        )
        conn.execute(text(module.UPGRADE_SQL))
        present = {
            r[0]
            for r in conn.execute(
                text("SELECT conname FROM pg_constraint WHERE conname LIKE 'ck_support_access%'")
            )
        }
        assert {name for name, _t, _b in module.CHECKS} <= present
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_removes_the_tables_and_the_upgrade_restores_them(db_session):
    """The downgrade is executed here, not merely read."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))  # idempotent both ways
        tables = set(inspect(conn).get_table_names())
        assert "support_access_grant" not in tables
        assert "support_access_use" not in tables

        conn.execute(text(module.UPGRADE_SQL))
        tables = set(inspect(conn).get_table_names())
        assert {"support_access_grant", "support_access_use"} <= tables
    finally:
        db_session.rollback()
