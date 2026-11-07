"""The due-request executor runs a scheduled erasure as the scope it names (#1105).

Before this, the executor called ``erase_user`` for every due grace-period entry, so a delayed
member erasure (an organization admin's request for one member's data in one organization) would
have run as the deletion of that person's whole account, across every organization.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.models.organization import Organization
from app.services import erasure_ledger_service as ledger
from app.services import gdpr_erasure_service
from app.services import scheduled_erasure_service as executor

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
DUE = NOW + timedelta(hours=73)


@pytest.fixture(autouse=True)
def journal_dir(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)


@pytest.fixture()
def calls(monkeypatch) -> list[tuple]:
    seen: list[tuple] = []

    def fake_user(db, user_id, **kwargs):
        seen.append(("user", user_id))
        return {"complete": True}

    def fake_member(db, user_id, org_id, **kwargs):
        seen.append(("org_member", user_id, org_id, kwargs.get("created_before")))
        return {"complete": True}

    def fake_org(db, org_id, **kwargs):
        seen.append(("organization", org_id))
        return {"complete": True}

    monkeypatch.setattr(gdpr_erasure_service, "erase_user", fake_user)
    monkeypatch.setattr(gdpr_erasure_service, "erase_org_member_data", fake_member)
    monkeypatch.setattr(gdpr_erasure_service, "erase_organization", fake_org)
    return seen


@pytest.fixture()
def org(db_session) -> Organization:
    row = Organization(external_org_id="org_sched_exec", name="Sched Exec Org", is_active=True)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _grace_entry(db, **subject):
    entry = ledger.record_request(
        db,
        actor_kind="org_admin",
        status="deferred",
        deferred_reason="grace_period",
        scheduled_for=NOW + timedelta(hours=72),
        requested_at=NOW,
        **subject,
    )
    assert entry is not None
    return entry


def test_a_due_member_erasure_erases_the_member_data_not_the_account(
    db_session, calls, normal_user, org
):
    entry = _grace_entry(
        db_session,
        subject_type="org_member",
        subject_user_id=normal_user.id,
        subject_user_uuid=normal_user.uuid,
        subject_organization_id=org.id,
        subject_organization_uuid=org.uuid,
    )

    assert executor._execute_one(db_session, entry.id, DUE) is True

    assert calls == [("org_member", normal_user.id, org.id, None)]
    db_session.refresh(entry)
    assert entry.status == "pending"


def test_a_due_organization_erasure_erases_the_organization(db_session, calls, org):
    entry = _grace_entry(
        db_session,
        subject_type="organization",
        subject_organization_id=org.id,
        subject_organization_uuid=org.uuid,
    )

    assert executor._execute_one(db_session, entry.id, DUE) is True

    assert calls == [("organization", org.id)]


def test_a_member_erasure_is_not_due_before_its_time(db_session, calls, normal_user, org):
    entry = _grace_entry(
        db_session,
        subject_type="org_member",
        subject_user_id=normal_user.id,
        subject_user_uuid=normal_user.uuid,
        subject_organization_id=org.id,
        subject_organization_uuid=org.uuid,
    )

    assert executor._execute_one(db_session, entry.id, NOW + timedelta(hours=71)) is False

    assert calls == []
    db_session.refresh(entry)
    assert entry.status == "deferred"


def test_the_last_super_admin_rule_does_not_block_a_member_erasure_of_one(
    db_session, calls, super_admin_user, org
):
    """The rule protects the ACCOUNT; a member erasure never deletes it."""
    entry = _grace_entry(
        db_session,
        subject_type="org_member",
        subject_user_id=super_admin_user.id,
        subject_user_uuid=super_admin_user.uuid,
        subject_organization_id=org.id,
        subject_organization_uuid=org.uuid,
    )

    assert executor._execute_one(db_session, entry.id, DUE) is True
    assert calls == [("org_member", super_admin_user.id, org.id, None)]
