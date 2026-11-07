"""Scheduling and cancelling an erasure (#1105): the ledger entry, its delay and its audit trail.

The governance events (``gdpr.erasure.scheduled`` / ``.cancelled`` / ``.execution_started``) are
staged in the audit outbox (v459) in the SAME transaction as the ledger change, so the change and
its record commit or roll back together.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.constants import GDPR_ORG_ERASURE_DELAY_HOURS_KEY
from app.core.constants import GDPR_ORG_MEMBER_ERASURE_DELAY_HOURS_KEY
from app.core.constants import MAX_GDPR_ORG_ERASURE_DELAY_HOURS
from app.core.constants import MAX_GDPR_SELF_ERASURE_GRACE_DAYS
from app.models.organization import Organization
from app.services import audit_outbox
from app.services import gdpr_erasure_service
from app.services import scheduled_erasure_service as scheduled
from app.services.system_settings_service import set_setting

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "AUDIT_LOG_ENABLED", True)
    # Delivery is the relay's job; these tests read the staged rows.
    monkeypatch.setattr(audit_outbox, "deliver_staged", lambda db: None)


@pytest.fixture()
def org(db_session) -> Organization:
    row = Organization(external_org_id="org_sched_svc", name="Sched Svc Org", is_active=True)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _events(db, resource_uuid) -> list[tuple[str, str | None, int | None]]:
    rows = db.execute(
        text(
            "SELECT event_type, payload->>'resource_uuid', "
            "(payload->>'organization_id')::int FROM audit_outbox "
            "WHERE payload->>'resource_uuid' = :u ORDER BY id"
        ),
        {"u": str(resource_uuid)},
    ).all()
    return [tuple(r) for r in rows]


def _schedule_member(db, admin, member, org, **kw):
    return scheduled.schedule(
        db,
        subject_type="org_member",
        actor=admin,
        actor_kind="org_admin",
        delay=timedelta(hours=72),
        subject_user=member,
        organization=org,
        now=kw.pop("now", NOW),
        **kw,
    )


def test_schedule_records_a_grace_entry_and_its_event_together(
    db_session, admin_user, normal_user, org
):
    entry = _schedule_member(db_session, admin_user, normal_user, org)

    assert (entry.status, entry.deferred_reason) == ("deferred", "grace_period")
    assert entry.scheduled_for == NOW + timedelta(hours=72)
    assert entry.request_digest is not None and len(entry.request_digest) == 64
    assert entry.actor_user_id == admin_user.id
    assert _events(db_session, entry.uuid) == [("gdpr.erasure.scheduled", str(entry.uuid), org.id)]


def test_a_failed_ledger_write_leaves_no_event_behind(
    db_session, admin_user, normal_user, org, monkeypatch
):
    """The event is staged in the ledger write's own transaction, so a failed write takes it too."""
    from app.services import erasure_ledger_service as ledger

    org_id = org.id
    count_sql = text(
        "SELECT count(*) FROM audit_outbox WHERE event_type = 'gdpr.erasure.scheduled' "
        "AND (payload->>'organization_id')::int = :org"
    )
    staged_before_write: list[int] = []

    def failing_record_request(db, **_kwargs):
        staged_before_write.append(db.execute(count_sql, {"org": org_id}).scalar_one())
        db.rollback()
        return None

    monkeypatch.setattr(ledger, "record_request", failing_record_request)

    with pytest.raises(scheduled.ScheduledErasureError) as refused:
        _schedule_member(db_session, admin_user, normal_user, org)

    assert refused.value.code == "ledger_unavailable"
    assert staged_before_write == [1], "the event must already be in the write's transaction"
    assert db_session.execute(count_sql, {"org": org_id}).scalar_one() == 0


def test_a_second_open_request_for_the_same_subject_is_refused(
    db_session, admin_user, normal_user, org
):
    _schedule_member(db_session, admin_user, normal_user, org)
    with pytest.raises(scheduled.ScheduledErasureError, match="already scheduled"):
        _schedule_member(db_session, admin_user, normal_user, org)


def test_a_member_erasure_and_the_persons_own_request_both_schedule(
    db_session, admin_user, normal_user, org
):
    member = _schedule_member(db_session, admin_user, normal_user, org)
    own = scheduled.schedule(
        db_session,
        subject_type="user",
        actor=normal_user,
        actor_kind="data_subject",
        delay=timedelta(days=14),
        subject_user=normal_user,
        now=NOW,
    )
    assert {member.subject_type, own.subject_type} == {"org_member", "user"}


@pytest.mark.parametrize("delay", [timedelta(days=MAX_GDPR_SELF_ERASURE_GRACE_DAYS, seconds=1)])
def test_no_scheduled_erasure_may_wait_past_the_self_erasure_cap(
    db_session, admin_user, org, delay
):
    with pytest.raises(ValueError, match="outside"):
        scheduled.schedule(
            db_session,
            subject_type="organization",
            actor=admin_user,
            actor_kind="org_admin",
            delay=delay,
            organization=org,
        )


def test_the_org_delay_setting_stays_inside_the_erasure_deadline_cap():
    """The org delay's ceiling must never exceed the cap that keeps grace + retry inside one month."""
    assert timedelta(hours=MAX_GDPR_ORG_ERASURE_DELAY_HOURS) <= scheduled.MAX_SCHEDULED_DELAY
    assert timedelta(days=MAX_GDPR_SELF_ERASURE_GRACE_DAYS) == scheduled.MAX_SCHEDULED_DELAY


@pytest.mark.parametrize(
    ("configured", "hours"), [(None, 72), ("1", 24), ("100", 100), ("100000", 168), ("x", 72)]
)
@pytest.mark.parametrize(
    ("subject_type", "key"),
    [
        ("organization", GDPR_ORG_ERASURE_DELAY_HOURS_KEY),
        ("org_member", GDPR_ORG_MEMBER_ERASURE_DELAY_HOURS_KEY),
    ],
)
def test_the_delay_is_read_per_scope_and_clamped(db_session, subject_type, key, configured, hours):
    if configured is not None:
        set_setting(db_session, key, configured)
    assert scheduled.delay_for(db_session, subject_type) == timedelta(hours=hours)


def test_cancel_before_the_time_withdraws_it_with_an_event(
    db_session, admin_user, normal_user, org
):
    entry = _schedule_member(db_session, admin_user, normal_user, org)

    cancelled = scheduled.cancel(db_session, entry, actor=admin_user, now=NOW + timedelta(hours=1))

    assert cancelled.status == "cancelled"
    assert [e[0] for e in _events(db_session, entry.uuid)] == [
        "gdpr.erasure.scheduled",
        "gdpr.erasure.cancelled",
    ]
    assert scheduled.open_request(db_session, "org_member", normal_user.id, org.id) is None


def test_cancel_after_the_time_is_refused(db_session, admin_user, normal_user, org):
    entry = _schedule_member(db_session, admin_user, normal_user, org)

    with pytest.raises(scheduled.ScheduledErasureError) as refused:
        scheduled.cancel(db_session, entry, actor=admin_user, now=NOW + timedelta(hours=72))

    assert refused.value.code == "already_executing"
    db_session.refresh(entry)
    assert entry.status == "deferred"


def test_execution_is_recorded_when_the_entry_is_handed_to_the_erasure(
    db_session, admin_user, normal_user, org, monkeypatch
):
    monkeypatch.setattr(
        gdpr_erasure_service, "erase_org_member_data", lambda *a, **k: {"complete": True}
    )
    entry = _schedule_member(db_session, admin_user, normal_user, org)

    assert scheduled._execute_one(db_session, entry.id, NOW + timedelta(hours=72)) is True

    assert [e[0] for e in _events(db_session, entry.uuid)] == [
        "gdpr.erasure.scheduled",
        "gdpr.erasure.execution_started",
    ]


def test_the_organizations_list_holds_only_its_own_open_entries(
    db_session, admin_user, normal_user, org
):
    other = Organization(external_org_id="org_sched_other", name="Other", is_active=True)
    db_session.add(other)
    db_session.commit()
    mine = _schedule_member(db_session, admin_user, normal_user, org)
    theirs = _schedule_member(db_session, admin_user, normal_user, other)

    assert [e.id for e in scheduled.open_entries_for_organization(db_session, org.id)] == [mine.id]
    assert scheduled.find_organization_entry(db_session, org.id, theirs.uuid) is None
    found = scheduled.find_organization_entry(db_session, org.id, mine.uuid)
    assert found is not None and found.id == mine.id
