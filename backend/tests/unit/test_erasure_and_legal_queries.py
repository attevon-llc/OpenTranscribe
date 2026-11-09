"""Erasure ledger read queries and the legal acceptance listing."""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from app.legal import acceptance
from app.legal import registry
from app.models.erasure import ErasureLedgerEntry
from app.models.erasure import ErasureReceipt
from app.services.erasure_receipt import read_queries
from tests.fixtures.tag_world import make_file
from tests.fixtures.tag_world import make_org
from tests.fixtures.tag_world import make_user


def _entry(db, *, subject_type="file", status="complete", org=None, requested_at=None):
    row = ErasureLedgerEntry(
        subject_type=subject_type,
        subject_organization_id=org.id if org else None,
        subject_organization_uuid=org.uuid if org else None,
        subject_file_uuid=uuid.uuid4() if subject_type == "file" else None,
        subject_user_id=987001 if subject_type == "user" else None,
        status=status,
        actor_kind="super_admin",
        sla_due_at=datetime.now(UTC) + timedelta(days=30),
        requested_at=requested_at or datetime.now(UTC),
        counters={},
    )
    db.add(row)
    db.commit()
    return row


def _receipt(db, entry, attempt, outcome, generated_at):
    row = ErasureReceipt(
        ledger_entry_id=entry.id,
        attempt=attempt,
        phase="immediate",
        outcome=outcome,
        deleted={},
        de_attributed={},
        retained={},
        ttl={},
        residual={},
        unverified={},
        inventory_version=1,
        digest="c" * 64,
        generated_at=generated_at,
    )
    db.add(row)
    db.commit()
    return row


def test_list_ledger_page_filters_pages_and_reports_latest_outcome(db_session):
    now = datetime.now(UTC)
    old = _entry(db_session, status="failed", requested_at=now - timedelta(days=2))
    new = _entry(db_session, status="complete", requested_at=now - timedelta(days=1))
    _receipt(db_session, new, 1, "residual", now - timedelta(hours=2))
    _receipt(db_session, new, 2, "verified", now - timedelta(hours=1))

    entries, total, latest = read_queries.list_ledger_page(
        db_session, status="complete", page=1, per_page=50
    )
    assert [e.id for e in entries] == [new.id]
    assert total == 1
    assert latest == {new.id: "verified"}

    second_page, total, _ = read_queries.list_ledger_page(
        db_session, status=None, page=2, per_page=1
    )
    assert total == 2
    assert [e.id for e in second_page] == [old.id]


def test_org_ledger_entry_hides_other_tenants_and_whole_organization_erasures(db_session):
    org, other = make_org(db_session, "a"), make_org(db_session, "b")
    mine = _entry(db_session, subject_type="file", org=org)
    whole_org = _entry(db_session, subject_type="organization", org=org)

    visible = read_queries.get_org_ledger_entry(db_session, mine.uuid, org.id)
    assert visible is not None
    assert visible.id == mine.id
    assert read_queries.get_org_ledger_entry(db_session, mine.uuid, other.id) is None
    assert read_queries.get_org_ledger_entry(db_session, whole_org.uuid, org.id) is None
    platform_view = read_queries.get_ledger_entry(db_session, whole_org.uuid)
    assert platform_view is not None
    assert platform_view.id == whole_org.id


def test_list_receipts_is_newest_first(db_session):
    now = datetime.now(UTC)
    entry = _entry(db_session)
    first = _receipt(db_session, entry, 1, "residual", now - timedelta(hours=2))
    second = _receipt(db_session, entry, 2, "verified", now - timedelta(hours=1))

    assert [r.id for r in read_queries.list_receipts(db_session, entry)] == [second.id, first.id]


def test_find_file_applies_the_organization_filter_only_when_given(db_session):
    org, other = make_org(db_session, "a"), make_org(db_session, "b")
    owner = make_user(db_session, "owner")
    media = make_file(db_session, owner, org.id)

    for scope in (None, org.id):
        found = read_queries.find_file(db_session, media.uuid, scope)
        assert found is not None
        assert found.id == media.id
    assert read_queries.find_file(db_session, media.uuid, other.id) is None


def test_list_acceptances_with_email_is_newest_first_and_outer_joined(db_session):
    doc = registry.list_documents()[0]
    org = make_org(db_session, "a")
    user = make_user(db_session, "acc")
    row, created = acceptance.record_acceptance(
        db_session, user.id, doc, "203.0.113.5", organization_id=org.id
    )
    assert created

    scoped = acceptance.list_acceptances_with_email(db_session, org.id)
    assert [(r.id, email) for r, email in scoped] == [(row.id, user.email)]
    assert acceptance.list_acceptances_with_email(db_session, org.id + 1_000_000) == []
    assert row.id in {r.id for r, _ in acceptance.list_acceptances_with_email(db_session)}


def test_acceptance_organization_uuid(db_session):
    org = make_org(db_session, "a")

    assert acceptance.organization_uuid(db_session, org.id) == org.uuid
    assert acceptance.organization_uuid(db_session, None) is None
