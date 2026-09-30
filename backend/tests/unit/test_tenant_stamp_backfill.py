"""The operator backfill for legacy tenant stamps (issue #1110).

``app.services.tenant_stamp_backfill`` re-runs the v430 revision's stamping rules and
prints the rows it cannot stamp. These tests run it against seeded rows inside the
test transaction; everything rolls back.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.services import tenant_stamp_backfill as backfill


def _user(conn) -> int:
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
            ),
            {"e": f"tsb_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _org(conn, *members: int) -> int:
    org_id = int(
        conn.execute(
            text(
                "INSERT INTO organization (uuid, external_org_id, name, is_active) "
                "VALUES (gen_random_uuid(), :x, 'tsb org', true) RETURNING id"
            ),
            {"x": f"tsb_{uuid_pkg.uuid4().hex[:10]}"},
        ).scalar()
    )
    for user_id in members:
        conn.execute(
            text(
                "INSERT INTO organization_membership (organization_id, user_id, role) "
                "VALUES (:o, :u, 'org:member')"
            ),
            {"o": org_id, "u": user_id},
        )
    return org_id


def _personal_file(conn, user_id: int) -> None:
    conn.execute(
        text(
            "INSERT INTO media_file (uuid, filename, storage_path, file_size, content_type, "
            "user_id) VALUES (gen_random_uuid(), 'tsb.mp3', :p, 1, 'audio/mpeg', :u)"
        ),
        {"p": f"tsb/{uuid_pkg.uuid4().hex}", "u": user_id},
    )


def _term(conn, user_id: int, created_at: datetime | None = None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO custom_vocabulary (user_id, term, domain, is_active, created_at) "
                "VALUES (:u, :t, 'general', true, COALESCE(:c, now())) RETURNING id"
            ),
            {"u": user_id, "t": f"tsb {uuid_pkg.uuid4().hex[:8]}", "c": created_at},
        ).scalar()
    )


def _org_of_term(conn, term_id: int) -> int | None:
    value = conn.execute(
        text("SELECT organization_id FROM custom_vocabulary WHERE id = :i"), {"i": term_id}
    ).scalar()
    return int(value) if value is not None else None


def test_dry_run_counts_but_changes_nothing(db_session):
    conn = db_session.connection()
    try:
        owner = _user(conn)
        _org(conn, owner)
        term = _term(conn, owner)

        report = backfill.backfill_tenant_stamps(conn, apply=False, created_before=None)

        assert report["applied"] is False
        assert report["stamped"]["custom_vocabulary"] >= 1
        assert _org_of_term(conn, term) is None
    finally:
        db_session.rollback()


def test_apply_requires_a_cutoff(db_session):
    with pytest.raises(ValueError, match="created_before"):
        backfill.backfill_tenant_stamps(db_session.connection(), apply=True, created_before=None)


def test_apply_stamps_rows_created_before_the_cutoff_only(db_session):
    conn = db_session.connection()
    try:
        owner = _user(conn)
        org = _org(conn, owner)
        cutoff = datetime.now(UTC)
        legacy = _term(conn, owner, cutoff - timedelta(days=1))
        recent = _term(conn, owner, cutoff + timedelta(minutes=1))

        report = backfill.backfill_tenant_stamps(conn, apply=True, created_before=cutoff)

        assert report["applied"] is True
        assert _org_of_term(conn, legacy) == org
        assert _org_of_term(conn, recent) is None
        again = backfill.backfill_tenant_stamps(conn, apply=True, created_before=cutoff)
        assert again["stamped"]["custom_vocabulary"] == 0, "a second run must be a no-op"
    finally:
        db_session.rollback()


def test_ambiguous_rows_are_reported_with_their_candidate_tenants(db_session):
    conn = db_session.connection()
    try:
        owner = _user(conn)
        org = _org(conn, owner)
        _personal_file(conn, owner)
        term = _term(conn, owner)

        report = backfill.backfill_tenant_stamps(conn, apply=False, created_before=None)

        [row] = [r for r in report["ambiguous"] if r["kind"] == "vocabulary" and r["id"] == term]
        assert row["reason"] == "owner_in_several_tenants"
        assert row["user_id"] == owner
        assert sorted(row["candidates"], key=str) == sorted([org, None], key=str)
        assert report["ambiguous_count"] == len(report["ambiguous"])
    finally:
        db_session.rollback()


class _FakeOpenSearch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def update_by_query(self, index: str, body: dict, **_kw) -> dict:
        self.calls.append((index, body))
        return {"updated": len(body["query"]["ids"]["values"])}


def test_voiceprint_sync_sets_the_tenant_on_profile_documents():
    client = _FakeOpenSearch()
    profile_orgs = {"u-1": 7, "u-2": 7, "u-3": 9}

    result = backfill.sync_profile_voiceprint_tenants(
        profile_orgs, client=client, indices=["speakers"]
    )

    by_org = {
        call[1]["script"]["params"]["org"]: sorted(call[1]["query"]["ids"]["values"])
        for call in client.calls
    }
    assert by_org == {7: ["profile_u-1", "profile_u-2"], 9: ["profile_u-3"]}
    assert result == {"updated": 3, "indices": ["speakers"]}
