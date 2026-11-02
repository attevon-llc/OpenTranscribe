"""``GET /api/admin/security/audit-integrity``: measured audit-record protection."""

from __future__ import annotations

import uuid as uuid_pkg

from sqlalchemy import text

from app.core.config import settings
from app.db.audit_protection import all_protected_tables
from app.db.roles import reconcile_app_role

URL = "/api/admin/security/audit-integrity"


def test_only_a_super_admin_may_call(client, admin_token_headers, user_token_headers):
    assert client.get(URL, headers=admin_token_headers).status_code == 403
    assert client.get(URL, headers=user_token_headers).status_code == 403


def test_as_the_owner_it_reports_not_enforced_and_why(client, super_admin_token_headers):
    response = client.get(URL, headers=super_admin_token_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enforced"] is False
    assert body["split_requested"] is False
    assert body["runtime_role_is_superuser"] is True
    assert {t["name"] for t in body["tables"]} == {t.name for t in all_protected_tables()}
    assert all(t["guard_trigger_present"] for t in body["tables"])
    assert "role" not in response.text.lower().replace("runtime_role_", "")


def test_as_a_reconciled_app_role_with_the_split_requested_it_reports_enforced(
    client, db_session, super_admin_token_headers, monkeypatch
):
    conn = db_session.connection()
    role = f"ot_pytest_{uuid_pkg.uuid4().hex[:10]}"
    conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
    reconcile_app_role(conn, role, None, provision=False)
    monkeypatch.setattr(settings, "POSTGRES_APP_USER", role)
    conn.execute(text(f'SET LOCAL ROLE "{role}"'))
    try:
        response = client.get(URL, headers=super_admin_token_headers)
    finally:
        conn.execute(text("RESET ROLE"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["split_requested"] is True
    assert body["enforced"] is True
    assert body["runtime_role_is_superuser"] is False
    assert body["runtime_role_owns_tables"] is False
    ledger = next(t for t in body["tables"] if t["name"] == "erasure_ledger")
    assert ledger["delete"] is False and ledger["truncate"] is False
    assert "subject_type" not in ledger["update_columns"]
