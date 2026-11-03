"""Seed one row in each protected audit table, and assert a statement is assert_refused (42501)."""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import InternalError


def seed_audit_rows(conn, user_id: int) -> dict[str, int]:
    """Insert one row per protected table (inside the caller's transaction); returns the ids."""
    ids: dict[str, int] = {}
    ids["auth_config_audit"] = conn.execute(
        text(
            "INSERT INTO auth_config_audit (uuid, config_key, change_type, changed_by, new_value)"
            " VALUES (:u, 'k', 'update', :uid, 'v') RETURNING id"
        ),
        {"u": uuid_pkg.uuid4(), "uid": user_id},
    ).scalar()
    grant = conn.execute(
        text(
            "INSERT INTO support_access_grant (uuid, target_kind, access_level, grant_mode,"
            " reason, requested_duration_minutes, decided_by_user_id)"
            " VALUES (:u, 'personal', 'read', 'approved', 'ten chars ok!', 60, :uid)"
            " RETURNING id"
        ),
        {"u": uuid_pkg.uuid4(), "uid": user_id},
    ).scalar()
    ids["support_access_grant"] = grant
    ids["support_access_use"] = conn.execute(
        text(
            "INSERT INTO support_access_use (grant_id, method, route)"
            " VALUES (:g, 'GET', '/x') RETURNING id"
        ),
        {"g": grant},
    ).scalar()
    ids["erasure_ledger"] = conn.execute(
        text(
            "INSERT INTO erasure_ledger (uuid, subject_type, subject_user_id, subject_user_uuid,"
            " sla_due_at, actor_user_id, actor_kind)"
            " VALUES (:u, 'user', :uid, :u, now() + interval '30 days', :uid, 'super_admin')"
            " RETURNING id"
        ),
        {"u": uuid_pkg.uuid4(), "uid": user_id},
    ).scalar()
    ids["erasure_receipt"] = conn.execute(
        text(
            "INSERT INTO erasure_receipt (uuid, ledger_entry_id, attempt, phase, outcome,"
            " inventory_version, digest) VALUES (:u, :l, 1, 'immediate', 'verified', 1, :d)"
            " RETURNING id"
        ),
        {"u": uuid_pkg.uuid4(), "l": ids["erasure_ledger"], "d": "0" * 64},
    ).scalar()
    notice = conn.execute(
        text(
            "INSERT INTO takedown_notice (uuid, notice_type, received_at, basis, requester_reference,"
            " handled_by_user_id) VALUES (:u, 'other', now(), 'basis', 'ref', :uid) RETURNING id"
        ),
        {"u": uuid_pkg.uuid4(), "uid": user_id},
    ).scalar()
    ids["takedown_notice"] = notice
    link = conn.execute(
        text(
            "INSERT INTO takedown_notice_file (uuid, notice_id, file_uuid, owner_user_uuid,"
            " owner_reason, quarantined_at) VALUES (:u, :n, :f, :o, 'reason', now())"
            " RETURNING id"
        ),
        {"u": uuid_pkg.uuid4(), "n": notice, "f": uuid_pkg.uuid4(), "o": uuid_pkg.uuid4()},
    ).scalar()
    ids["takedown_notice_file"] = link
    ids["takedown_notification"] = conn.execute(
        text(
            "INSERT INTO takedown_notification (link_id, event, recipient_kind,"
            " recipient_user_id, recipient_user_uuid, channel, status)"
            " VALUES (:l, 'quarantined', 'owner', :uid, :ru, 'in_app', 'sent') RETURNING id"
        ),
        {"l": link, "uid": user_id, "ru": uuid_pkg.uuid4()},
    ).scalar()
    return ids


@contextmanager
def assert_refused(conn):
    """Run the body in a savepoint and assert it fails with SQLSTATE 42501."""
    nested = conn.begin_nested()
    try:
        yield
    except (DBAPIError, InternalError) as exc:
        nested.rollback()
        assert getattr(exc.orig, "pgcode", None) == "42501", exc
        return
    nested.rollback()
    raise AssertionError("statement was not assert_refused")
