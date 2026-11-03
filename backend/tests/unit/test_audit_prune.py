"""audit_prune(): the one way the application role deletes audit rows, with a hard floor."""

from __future__ import annotations

import uuid as uuid_pkg

from sqlalchemy import text

from app.core.constants import AUDIT_RETENTION_FLOOR_DAYS
from app.db.roles import reconcile_app_role
from app.tasks.audit_retention import prune_audit_tables
from tests.unit._audit_rows import assert_refused
from tests.unit._audit_rows import seed_audit_rows


def _insert_audit(conn, age_days: int) -> int:
    row_id: int = conn.execute(
        text(
            "INSERT INTO auth_config_audit (uuid, config_key, change_type, created_at)"
            " VALUES (:u, 'prune-test', 'update', now() - make_interval(days => :d))"
            " RETURNING id"
        ),
        {"u": uuid_pkg.uuid4(), "d": age_days},
    ).scalar_one()
    return row_id


def _exists(conn, row_id: int) -> bool:
    return bool(
        conn.execute(
            text("SELECT count(*) FROM auth_config_audit WHERE id = :i"), {"i": row_id}
        ).scalar()
    )


def test_the_floor_holds_even_when_asked_for_one_day(db_session):
    conn = db_session.connection()
    old = _insert_audit(conn, AUDIT_RETENTION_FLOOR_DAYS + 30)
    young = _insert_audit(conn, 100)
    removed = conn.execute(text("SELECT audit_prune('auth_config_audit'::regclass, 1)")).scalar()
    assert removed >= 1
    assert not _exists(conn, old)
    assert _exists(conn, young), "a row younger than the floor must survive keep_days=1"


def test_the_app_role_can_only_delete_through_the_function(db_session, normal_user):
    conn = db_session.connection()
    role = f"ot_pytest_{uuid_pkg.uuid4().hex[:10]}"
    conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
    reconcile_app_role(conn, role, None, provision=False)
    old = _insert_audit(conn, AUDIT_RETENTION_FLOOR_DAYS + 30)
    seed_audit_rows(conn, normal_user.id)
    conn.execute(text(f'SET LOCAL ROLE "{role}"'))
    try:
        with assert_refused(conn):
            conn.execute(text("DELETE FROM auth_config_audit WHERE id = :i"), {"i": old})
        removed = prune_audit_tables(db_session, 1)
    finally:
        conn.execute(text("RESET ROLE"))
    assert removed["auth_config_audit"] >= 1
    assert not _exists(conn, old)


def test_tables_outside_the_prunable_list_are_refused(db_session, normal_user):
    conn = db_session.connection()
    seed_audit_rows(conn, normal_user.id)
    for table in ("erasure_ledger", "takedown_notice", "support_access_grant", "legal_acceptance"):
        with assert_refused(conn):
            conn.execute(text(f"SELECT audit_prune('{table}'::regclass, 4000)"))
    assert conn.execute(text("SELECT count(*) FROM erasure_ledger")).scalar() >= 1


def test_the_sweep_task_does_nothing_while_retention_is_keep_forever(monkeypatch):
    from app.core.config import settings
    from app.tasks.audit_retention import audit_retention_sweep

    monkeypatch.setattr(settings, "AUDIT_LOG_RETENTION_DAYS", 0)
    assert audit_retention_sweep.run() == {"status": "disabled", "removed": {}}
