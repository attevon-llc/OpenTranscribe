"""Layer 1: the immutability trigger on the protected audit tables (real Postgres).

Runs as the table owner inside the db_session transaction, so it proves the trigger itself
(not a privilege). Layer 2 (the app role) is in ``test_db_roles_reconcile.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db.audit_protection import all_protected_tables
from app.db.audit_protection import generic_guard_tables
from tests.unit._audit_rows import assert_refused
from tests.unit._audit_rows import seed_audit_rows

GENERIC = [t.name for t in generic_guard_tables()]


def test_owner_update_of_immutable_column_is_refused(db_session, normal_user):
    conn = db_session.connection()
    ids = seed_audit_rows(conn, normal_user.id)
    with assert_refused(conn):
        conn.execute(
            text("UPDATE auth_config_audit SET new_value = 'x' WHERE id = :i"),
            {"i": ids["auth_config_audit"]},
        )
    with assert_refused(conn):
        conn.execute(
            text("UPDATE support_access_use SET route = '/y' WHERE id = :i"),
            {"i": ids["support_access_use"]},
        )


@pytest.mark.ddl_exclusive
@pytest.mark.parametrize("table", GENERIC)
def test_owner_delete_and_truncate_are_refused(db_session, normal_user, table):
    conn = db_session.connection()
    ids = seed_audit_rows(conn, normal_user.id)
    with assert_refused(conn):
        conn.execute(text(f"DELETE FROM {table} WHERE id = :i"), {"i": ids[table]})
    with assert_refused(conn):
        conn.execute(text(f"TRUNCATE {table} CASCADE"))
    assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() >= 1


def test_lifecycle_column_update_is_allowed_and_identity_column_is_not(db_session, normal_user):
    conn = db_session.connection()
    ids = seed_audit_rows(conn, normal_user.id)
    conn.execute(
        text("UPDATE erasure_ledger SET status = 'complete', completed_at = now() WHERE id = :i"),
        {"i": ids["erasure_ledger"]},
    )
    assert (
        conn.execute(
            text("SELECT status FROM erasure_ledger WHERE id = :i"), {"i": ids["erasure_ledger"]}
        ).scalar()
        == "complete"
    )
    with assert_refused(conn):
        conn.execute(
            text("UPDATE erasure_ledger SET subject_type = 'organization' WHERE id = :i"),
            {"i": ids["erasure_ledger"]},
        )
    with assert_refused(conn):
        conn.execute(
            text("UPDATE erasure_ledger SET subject_user_uuid = gen_random_uuid() WHERE id = :i"),
            {"i": ids["erasure_ledger"]},
        )


def test_a_lifecycle_actor_column_may_be_set_after_being_cleared(db_session, normal_user):
    """decided_by_user_id is both an actor (cleared on user deletion) and a lifecycle column
    (set when a grant is decided): the lifecycle half must not be blocked by the clear-only rule."""
    conn = db_session.connection()
    ids = seed_audit_rows(conn, normal_user.id)
    row = {"i": ids["support_access_grant"], "u": normal_user.id}
    conn.execute(
        text("UPDATE support_access_grant SET decided_by_user_id = NULL WHERE id = :i"), row
    )
    conn.execute(text("UPDATE support_access_grant SET decided_by_user_id = :u WHERE id = :i"), row)
    assert (
        conn.execute(
            text("SELECT decided_by_user_id FROM support_access_grant WHERE id = :i"), row
        ).scalar()
        == normal_user.id
    )


def test_actor_column_may_be_cleared_but_not_rewritten(db_session, normal_user, admin_user):
    conn = db_session.connection()
    ids = seed_audit_rows(conn, normal_user.id)
    row = {"i": ids["auth_config_audit"]}
    with assert_refused(conn):
        conn.execute(
            text("UPDATE auth_config_audit SET changed_by = :o WHERE id = :i"),
            {**row, "o": admin_user.id},
        )
    conn.execute(text("UPDATE auth_config_audit SET changed_by = NULL WHERE id = :i"), row)


def test_maintenance_switch_works_for_the_owner(db_session, normal_user):
    conn = db_session.connection()
    ids = seed_audit_rows(conn, normal_user.id)
    conn.execute(text("SELECT set_config('opentranscribe.audit_maintenance', 'on', true)"))
    conn.execute(
        text("UPDATE auth_config_audit SET new_value = 'repaired' WHERE id = :i"),
        {"i": ids["auth_config_audit"]},
    )
    conn.execute(
        text("DELETE FROM support_access_use WHERE id = :i"), {"i": ids["support_access_use"]}
    )
    conn.execute(text("SELECT set_config('opentranscribe.audit_maintenance', 'off', true)"))
    with assert_refused(conn):
        conn.execute(
            text("DELETE FROM auth_config_audit WHERE id = :i"), {"i": ids["auth_config_audit"]}
        )


def test_deleting_a_user_nulls_every_actor_column_and_keeps_the_audit_rows(db_session, normal_user):
    """The GDPR-erasure regression guard: ON DELETE SET NULL must pass through the trigger."""
    conn = db_session.connection()
    ids = seed_audit_rows(conn, normal_user.id)
    conn.execute(text('DELETE FROM "user" WHERE id = :u'), {"u": normal_user.id})
    checks = {
        "auth_config_audit": "changed_by",
        "erasure_ledger": "actor_user_id",
        "takedown_notice": "handled_by_user_id",
        "takedown_notification": "recipient_user_id",
        "support_access_grant": "decided_by_user_id",
    }
    for table, column in checks.items():
        row = conn.execute(
            text(f"SELECT {column} FROM {table} WHERE id = :i"), {"i": ids[table]}
        ).one_or_none()
        assert row is not None, f"{table} row was destroyed by the user deletion"
        assert row[0] is None, f"{table}.{column} was not cleared"


def test_cascade_delete_of_a_takedown_notice_is_refused(db_session, normal_user):
    conn = db_session.connection()
    ids = seed_audit_rows(conn, normal_user.id)
    with assert_refused(conn):
        conn.execute(
            text("DELETE FROM takedown_notice WHERE id = :i"), {"i": ids["takedown_notice"]}
        )


def test_every_protected_table_has_a_guard_trigger(db_session):
    conn = db_session.connection()
    found = set(
        conn.execute(
            text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname LIKE 'trg_%'")
        ).scalars()
    )
    assert len(all_protected_tables()) >= 8
    for t in all_protected_tables():
        assert (t.own_guard or f"trg_audit_guard_{t.name}") in found
        if not t.own_guard:
            assert f"trg_audit_guard_truncate_{t.name}" in found
