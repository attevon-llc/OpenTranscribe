"""Layer 2: the unprivileged application role, against a real Postgres.

Every test creates its own role inside the ``db_session`` transaction (``CREATE ROLE`` is
transactional), so nothing persists. The role is entered with ``SET LOCAL ROLE``.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from sqlalchemy import event
from sqlalchemy import text

from app.core.config import settings
from app.db.audit_protection import all_protected_tables
from app.db.base import engine as test_engine
from app.db.role_check import enforce_runtime_role
from app.db.role_check import measure_role
from app.db.roles import reconcile_app_role
from tests.unit._audit_rows import assert_refused
from tests.unit._audit_rows import seed_audit_rows


def _new_role(conn, *, login: bool = False, superuser: bool = False) -> str:
    role = f"ot_pytest_{uuid_pkg.uuid4().hex[:10]}"
    conn.execute(
        text(
            f'CREATE ROLE "{role}" {"LOGIN" if login else "NOLOGIN"} '
            f"{'SUPERUSER' if superuser else 'NOSUPERUSER'}"
        )
    )
    return role


def _app_role(conn) -> str:
    role = _new_role(conn)
    reconcile_app_role(conn, role, None, provision=False)
    return role


@pytest.mark.ddl_exclusive
def test_app_role_cannot_update_delete_truncate_protected_tables_even_without_the_trigger(
    db_session, normal_user
):
    """The grants alone hold: the triggers are dropped inside the transaction."""
    conn = db_session.connection()
    ids = seed_audit_rows(conn, normal_user.id)
    role = _app_role(conn)
    for table in all_protected_tables():
        if not table.own_guard:
            conn.execute(text(f"DROP TRIGGER trg_audit_guard_{table.name} ON {table.name}"))
            conn.execute(
                text(f"DROP TRIGGER trg_audit_guard_truncate_{table.name} ON {table.name}")
            )
    conn.execute(text(f'SET LOCAL ROLE "{role}"'))
    try:
        conn.execute(  # INSERT is allowed
            text(
                "INSERT INTO auth_config_audit (uuid, config_key, change_type)"
                " VALUES (gen_random_uuid(), 'k2', 'create')"
            )
        )
        with assert_refused(conn):
            conn.execute(text("UPDATE auth_config_audit SET new_value = 'x'"))
        with assert_refused(conn):
            conn.execute(text("DELETE FROM auth_config_audit"))
        with assert_refused(conn):
            conn.execute(text("TRUNCATE auth_config_audit"))
        with assert_refused(conn):
            conn.execute(text("UPDATE erasure_ledger SET subject_type = 'organization'"))
        with assert_refused(conn):
            conn.execute(text("DELETE FROM takedown_notice"))
        # Lifecycle column: allowed by the column grant.
        conn.execute(
            text("UPDATE erasure_ledger SET status = 'complete' WHERE id = :i"),
            {"i": ids["erasure_ledger"]},
        )
    finally:
        conn.execute(text("RESET ROLE"))


def test_user_deletion_as_the_app_role_keeps_the_audit_rows(db_session, normal_user):
    """GDPR erasure regression guard: the app role has no UPDATE, yet ON DELETE SET NULL works
    because Postgres runs referential actions as the owner of the referencing table."""
    conn = db_session.connection()
    ids = seed_audit_rows(conn, normal_user.id)
    role = _app_role(conn)
    conn.execute(text(f'SET LOCAL ROLE "{role}"'))
    try:
        conn.execute(text('DELETE FROM "user" WHERE id = :u'), {"u": normal_user.id})
    finally:
        conn.execute(text("RESET ROLE"))
    for table, column in (
        ("auth_config_audit", "changed_by"),
        ("erasure_ledger", "actor_user_id"),
        ("takedown_notification", "recipient_user_id"),
    ):
        row = conn.execute(
            text(f"SELECT {column} FROM {table} WHERE id = :i"), {"i": ids[table]}
        ).one_or_none()
        assert row is not None and row[0] is None


def test_the_maintenance_switch_buys_the_app_role_nothing(db_session, normal_user):
    conn = db_session.connection()
    seed_audit_rows(conn, normal_user.id)
    role = _app_role(conn)
    conn.execute(text(f'SET LOCAL ROLE "{role}"'))
    try:
        conn.execute(text("SELECT set_config('opentranscribe.audit_maintenance', 'on', true)"))
        with assert_refused(conn):
            conn.execute(text("UPDATE support_access_use SET route = '/z'"))
        with assert_refused(conn):
            conn.execute(text("DELETE FROM support_access_use"))
    finally:
        conn.execute(text("RESET ROLE"))


def test_a_correctly_reconciled_role_measures_clean(db_session):
    conn = db_session.connection()
    role = _app_role(conn)
    conn.execute(text(f'SET LOCAL ROLE "{role}"'))
    try:
        state = measure_role(conn)
    finally:
        conn.execute(text("RESET ROLE"))
    assert state.problems == []
    assert not state.is_superuser and not state.owns_tables
    assert {t.name for t in state.tables} == {t.name for t in all_protected_tables()}
    ledger = next(t for t in state.tables if t.name == "erasure_ledger")
    assert ledger.insert and not ledger.delete and not ledger.truncate
    assert "status" in ledger.update_columns and "subject_type" not in ledger.update_columns


def test_reconcile_restores_grants_after_a_no_acl_restore_and_is_idempotent(db_session):
    conn = db_session.connection()
    role = _app_role(conn)
    conn.execute(text(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{role}"'))
    assert not conn.execute(
        text("SELECT has_table_privilege(:r, 'auth_config_audit', 'INSERT')"), {"r": role}
    ).scalar()
    reconcile_app_role(conn, role, None, provision=False)
    reconcile_app_role(conn, role, None, provision=False)
    assert conn.execute(
        text("SELECT has_table_privilege(:r, 'auth_config_audit', 'INSERT')"), {"r": role}
    ).scalar()
    assert not conn.execute(
        text("SELECT has_table_privilege(:r, 'auth_config_audit', 'UPDATE')"), {"r": role}
    ).scalar()
    assert conn.execute(
        text("SELECT has_table_privilege(:r, 'media_file', 'DELETE')"), {"r": role}
    ).scalar()


def test_password_is_sent_as_a_scram_verifier_never_in_clear(db_session):
    conn = db_session.connection()
    role = f"ot_pytest_{uuid_pkg.uuid4().hex[:10]}"
    plaintext = "Pl41n-Secret-" + uuid_pkg.uuid4().hex
    seen: list[str] = []

    def capture(_c, _cur, statement, parameters, _ctx, _many):
        seen.append(f"{statement} {parameters}")

    event.listen(conn, "before_cursor_execute", capture)
    try:
        reconcile_app_role(conn, role, plaintext, provision=True)
    finally:
        event.remove(conn, "before_cursor_execute", capture)
    blob = "\n".join(seen)
    assert "SCRAM-SHA-256$" in blob
    assert plaintext not in blob
    assert (
        conn.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
        is False
    )


def test_a_superuser_runtime_role_is_flagged_and_refused(db_session, monkeypatch):
    conn = db_session.connection()
    role = _new_role(conn, superuser=True)
    conn.execute(text(f'SET LOCAL ROLE "{role}"'))
    try:
        state = measure_role(conn)
    finally:
        conn.execute(text("RESET ROLE"))
    assert state.is_superuser
    assert any("superuser" in p for p in state.problems)

    # The boot check: the test engine connects as the owner (a superuser here).
    monkeypatch.setattr(settings, "POSTGRES_APP_USER", "requested_but_not_in_use")
    with pytest.raises(RuntimeError, match="not effective"):
        enforce_runtime_role(test_engine)


def test_boot_check_is_a_no_op_when_the_split_was_not_requested(monkeypatch):
    monkeypatch.setattr(settings, "POSTGRES_APP_USER", "")
    monkeypatch.setattr(settings, "MIGRATION_DATABASE_URL", settings.DATABASE_URL)
    assert settings.db_role_split_requested is False
    enforce_runtime_role(test_engine)  # would raise if it ran: the owner is not an app role
