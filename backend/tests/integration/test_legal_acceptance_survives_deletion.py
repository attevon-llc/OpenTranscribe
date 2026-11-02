"""Legal acceptance records outlive the account, pseudonymised (v451).

Runs against the real Postgres in the test savepoint: the behaviour under test is a foreign-key
rule plus a trigger, so nothing short of the database can show it. Every deletion path is
covered: the admin route, ``erase_user`` and a raw ``DELETE FROM "user"`` (the first-run
wizard's shape, which bypasses every service).
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.models.legal_acceptance import LegalAcceptance
from app.models.organization import Organization
from app.models.user import User

pytestmark = pytest.mark.integration

_IP = "203.0.113.77"
_UA = "LegalRecTestAgent/1.0"


def _user(db) -> User:
    tag = uuid_pkg.uuid4().hex[:10]
    u = User(
        email=f"legalrec-{tag}@example.com",
        full_name=f"Legalrec Person {tag}",
        hashed_password="x",
        role="user",
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def _accept(db, user: User, *, scope: str = "user", org: Organization | None = None):
    row = LegalAcceptance(
        user_id=user.id,
        document_key="opentranscribe-terms-of-use",
        version="1",
        sha256=uuid_pkg.uuid4().hex * 2,
        ip_address=_IP,
        user_agent=_UA,
        locale="en-GB",
        scope=scope,
        organization_id=org.id if org else None,
        organization_uuid=org.uuid if org else None,
    )
    db.add(row)
    db.flush()
    return row


def _row(db, row_id: int):
    return (
        db.execute(text("SELECT * FROM legal_acceptance WHERE id = :i"), {"i": row_id})
        .mappings()
        .one_or_none()
    )


def _row_text(db, row_id: int) -> str:
    """Every column of the row as one string, for a generic personal-data scan."""
    whole: str = db.execute(
        text("SELECT la::text FROM legal_acceptance la WHERE id = :i"), {"i": row_id}
    ).scalar_one()
    return whole


def assert_pseudonymised(db, row_id: int, user_uuid, *, personal: list[str | None]) -> None:
    """The documented survivor state, and nothing of the person beyond the surrogate uuid."""
    r = _row(db, row_id)
    assert r is not None, "the record must survive the account"
    assert r["user_id"] is None
    assert r["ip_address"] is None and r["user_agent"] is None
    assert r["subject_erased_at"] is not None
    assert r["retain_until"] is not None and r["retain_until"] > r["subject_erased_at"]
    assert r["subject_user_uuid"] == user_uuid
    leaked = [p for p in personal if p and p in _row_text(db, row_id)]
    assert leaked == [], f"personal data kept beyond the retention policy: {leaked}"


def _raw_delete_user(db, user_id: int) -> None:
    db.execute(text('DELETE FROM "user" WHERE id = :u'), {"u": user_id})
    db.flush()


def test_raw_user_delete_keeps_the_record_pseudonymised(db_session):
    """The first-run wizard's path: no service runs, only the database rule."""
    u = _user(db_session)
    row = _accept(db_session, u)
    email, name, uuid = u.email, u.full_name, u.uuid
    db_session.expunge(u)
    _raw_delete_user(db_session, u.id)
    assert_pseudonymised(db_session, row.id, uuid, personal=[email, name, _IP, _UA])


def test_the_personal_data_scan_fires_on_a_row_that_kept_an_ip(db_session):
    """Must-fire control for the scan above: a pseudonymised row that still holds a copy of
    the IP (planted with triggers off) is caught by the generic text scan."""
    u = _user(db_session)
    row = _accept(db_session, u)
    uuid = u.uuid
    db_session.expunge(u)
    _raw_delete_user(db_session, u.id)
    db_session.execute(text("SET LOCAL session_replication_role = replica"))
    db_session.execute(
        text("UPDATE legal_acceptance SET locale = :ip WHERE id = :i"), {"ip": _IP, "i": row.id}
    )
    db_session.execute(text("SET LOCAL session_replication_role = origin"))
    with pytest.raises(AssertionError, match="personal data kept beyond the retention policy"):
        assert_pseudonymised(db_session, row.id, uuid, personal=[_IP])


def test_admin_delete_keeps_the_record(client, db_session, admin_token_headers):
    u = _user(db_session)
    row = _accept(db_session, u)
    email, uuid = u.email, u.uuid
    db_session.commit()
    r = client.delete(f"/api/admin/users/{u.uuid}", headers=admin_token_headers)
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert_pseudonymised(db_session, row.id, uuid, personal=[email, _IP, _UA])


def test_erase_user_reports_and_keeps_the_record(db_session, monkeypatch):
    from app.services import gdpr_erasure_service

    u = _user(db_session)
    rows = [_accept(db_session, u), _accept(db_session, u)]
    email, uuid = u.email, u.uuid
    db_session.commit()
    summary = gdpr_erasure_service.erase_user(db_session, int(u.id))
    assert summary["users_deleted"] == 1, summary
    assert summary["legal_acceptances_pseudonymised"] == 2
    db_session.expire_all()
    for row in rows:
        assert_pseudonymised(db_session, row.id, uuid, personal=[email, _IP, _UA])


def test_the_retention_period_comes_from_the_setting_with_a_floor(db_session):
    from app.services.system_settings_service import set_setting

    set_setting(db_session, "legal.acceptance_retention_days", "30")  # below the floor
    u = _user(db_session)
    row = _accept(db_session, u)
    _raw_delete_user(db_session, u.id)
    r = _row(db_session, row.id)
    assert r["retain_until"] - r["subject_erased_at"] == timedelta(days=365)

    set_setting(db_session, "legal.acceptance_retention_days", "1000")
    u2 = _user(db_session)
    row2 = _accept(db_session, u2)
    _raw_delete_user(db_session, u2.id)
    r2 = _row(db_session, row2.id)
    assert r2["retain_until"] - r2["subject_erased_at"] == timedelta(days=1000)


def test_the_default_retention_is_six_years(db_session):
    db_session.execute(
        text("DELETE FROM system_settings WHERE key = 'legal.acceptance_retention_days'")
    )
    u = _user(db_session)
    row = _accept(db_session, u)
    _raw_delete_user(db_session, u.id)
    r = _row(db_session, row.id)
    assert r["retain_until"] - r["subject_erased_at"] == timedelta(days=2190)


def test_the_accepted_text_cannot_be_rewritten(db_session):
    u = _user(db_session)
    row = _accept(db_session, u)
    with pytest.raises(DBAPIError, match="append-only"):
        with db_session.begin_nested():
            db_session.execute(
                text("UPDATE legal_acceptance SET sha256 = :s WHERE id = :i"),
                {"s": "f" * 64, "i": row.id},
            )
    with pytest.raises(DBAPIError, match="append-only"):
        with db_session.begin_nested():
            db_session.execute(
                text("UPDATE legal_acceptance SET ip_address = NULL WHERE id = :i"), {"i": row.id}
            )


def test_delete_needs_the_sweep_flag_and_a_passed_deadline(db_session):
    u = _user(db_session)
    row = _accept(db_session, u)
    with pytest.raises(DBAPIError, match="retention sweep"):
        with db_session.begin_nested():
            db_session.execute(text("DELETE FROM legal_acceptance WHERE id = :i"), {"i": row.id})

    _raw_delete_user(db_session, u.id)  # retain_until is now in the future
    with pytest.raises(DBAPIError, match="retention sweep"):
        with db_session.begin_nested():
            db_session.execute(text("SET LOCAL opentranscribe.legal_retention_sweep = 'on'"))
            db_session.execute(text("DELETE FROM legal_acceptance WHERE id = :i"), {"i": row.id})


def test_an_org_acceptance_survives_the_admin_and_the_org(db_session):
    org = Organization(external_org_id=f"org_lr_{uuid_pkg.uuid4().hex[:8]}", name="lr")
    db_session.add(org)
    db_session.flush()
    admin = _user(db_session)
    row = _accept(db_session, admin, scope="organization", org=org)
    org_uuid, admin_uuid = org.uuid, admin.uuid
    db_session.expunge(admin)
    _raw_delete_user(db_session, admin.id)
    db_session.execute(text("DELETE FROM organization WHERE id = :o"), {"o": org.id})
    db_session.flush()
    r = _row(db_session, row.id)
    assert r["scope"] == "organization"
    assert r["organization_id"] is None and r["organization_uuid"] == org_uuid
    assert_pseudonymised(db_session, row.id, admin_uuid, personal=[_IP, _UA])


def test_a_live_row_records_the_subject_uuid_at_insert(db_session):
    u = _user(db_session)
    row = _accept(db_session, u)
    assert _row(db_session, row.id)["subject_user_uuid"] == u.uuid


def test_admin_list_shows_a_pseudonymised_row_without_an_email(
    client, db_session, admin_token_headers
):
    u = _user(db_session)
    row = _accept(db_session, u)
    db_session.expunge(u)
    _raw_delete_user(db_session, u.id)
    db_session.commit()
    rows = client.get("/api/legal/acknowledgments", headers=admin_token_headers).json()
    mine = [r for r in rows if r["sha256"] == row.sha256]
    assert len(mine) == 1
    assert mine[0]["user_email"] is None and mine[0]["erased"] is True
    assert mine[0]["retain_until"] is not None


def test_the_sweep_deletes_only_rows_past_their_deadline(db_session):
    from app.tasks.legal_retention import delete_due_acceptances

    u1, u2, live = _user(db_session), _user(db_session), _user(db_session)
    due, future, keep = _accept(db_session, u1), _accept(db_session, u2), _accept(db_session, live)
    db_session.expunge_all()
    _raw_delete_user(db_session, u1.id)
    _raw_delete_user(db_session, u2.id)
    # Age the first one past its deadline. The guard refuses this UPDATE, so it runs with
    # triggers off for this transaction only (rolled back with the test).
    db_session.execute(text("SET LOCAL session_replication_role = replica"))
    past = datetime.now(UTC) - timedelta(days=1)
    db_session.execute(
        text("UPDATE legal_acceptance SET retain_until = :t, subject_erased_at = :e WHERE id = :i"),
        {"t": past, "e": past - timedelta(days=400), "i": due.id},
    )
    db_session.execute(text("SET LOCAL session_replication_role = origin"))

    assert delete_due_acceptances(db_session, limit=500) >= 1
    assert _row(db_session, due.id) is None
    assert _row(db_session, future.id) is not None
    assert _row(db_session, keep.id) is not None
    # The flag was SET LOCAL: without a fresh one a plain DELETE is still refused.
    db_session.execute(text("RESET opentranscribe.legal_retention_sweep"))
    with pytest.raises(DBAPIError, match="retention sweep"):
        with db_session.begin_nested():
            db_session.execute(text("DELETE FROM legal_acceptance WHERE id = :i"), {"i": keep.id})
