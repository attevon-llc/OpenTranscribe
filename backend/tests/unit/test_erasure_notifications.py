"""Notices of a scheduled organization or member erasure (#1105).

Four moments reach the organization's admins and, for a member erasure, the member: the request,
a reminder 24 hours before, the cancellation and the completion.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from unittest.mock import create_autospec

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.erasure import ErasureLedgerEntry
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.services import audit_outbox
from app.services import erasure_notifications as notices
from app.services import gdpr_erasure_service
from app.services import scheduled_erasure_service as scheduled
from app.services.erasure_notifications import ErasureNoticeKind

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class _FakeRedis:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.keys:
            return False
        self.keys.add(key)
        return True


def _record(into: list[Any], item: Any) -> bool:
    into.append(item)
    return True


@pytest.fixture()
def sent(monkeypatch, tmp_path) -> dict[str, list[Any]]:
    """Capture every websocket frame and queued mail; the erasure itself is stubbed."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(audit_outbox, "deliver_staged", lambda db: None)
    frames: list[tuple[int, str, dict]] = []
    mails: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "app.utils.websocket_notify.send_ws_event",
        lambda user_id, event, data: _record(frames, (user_id, event, data)),
    )
    monkeypatch.setattr(
        "app.core.auth_mail.dispatch_auth_mail",
        lambda task, email, subject, message: _record(mails, (email, subject, message)),
    )
    monkeypatch.setattr("app.services.email_service.email_service.has_transport", lambda: True)
    fake = _FakeRedis()
    monkeypatch.setattr(notices, "get_redis", lambda: fake)
    monkeypatch.setattr(
        gdpr_erasure_service, "erase_org_member_data", lambda *a, **k: {"complete": True}
    )
    monkeypatch.setattr(
        gdpr_erasure_service, "erase_organization", lambda *a, **k: {"complete": True}
    )
    return {"frames": frames, "mails": mails}


@pytest.fixture()
def org(db_session, admin_user, normal_user) -> Organization:
    row = Organization(external_org_id="org_erasure_notice", name="Notice Org", is_active=True)
    db_session.add(row)
    db_session.commit()
    db_session.add(
        OrganizationMembership(organization_id=row.id, user_id=admin_user.id, role="org:admin")
    )
    db_session.add(
        OrganizationMembership(organization_id=row.id, user_id=normal_user.id, role="org:member")
    )
    db_session.commit()
    db_session.refresh(row)
    return row


def _schedule(db, admin, member, org, delay_hours=72):
    return scheduled.schedule(
        db,
        subject_type="org_member",
        actor=admin,
        actor_kind="org_admin",
        delay=timedelta(hours=delay_hours),
        subject_user=member,
        organization=org,
        now=NOW,
    )


def _kinds(sent) -> list[tuple[int, str]]:
    return sorted((uid, d["event"]) for uid, _, d in sent["frames"])


def test_a_request_tells_the_admins_and_the_member_by_websocket_and_mail(
    db_session, sent, admin_user, normal_user, org
):
    _schedule(db_session, admin_user, normal_user, org)

    assert _kinds(sent) == sorted([(admin_user.id, "requested"), (normal_user.id, "requested")])
    assert all(event == "erasure_notice" for _, event, _ in sent["frames"])
    assert {m[0] for m in sent["mails"]} == {admin_user.email, normal_user.email}


def test_an_organization_erasure_does_not_tell_a_plain_member(
    db_session, sent, admin_user, normal_user, org
):
    scheduled.schedule(
        db_session,
        subject_type="organization",
        actor=admin_user,
        actor_kind="org_admin",
        delay=timedelta(hours=72),
        organization=org,
        now=NOW,
    )

    assert _kinds(sent) == [(admin_user.id, "requested")]


def test_cancelling_tells_them_nothing_was_deleted(db_session, sent, admin_user, normal_user, org):
    entry = _schedule(db_session, admin_user, normal_user, org)
    sent["frames"].clear()

    scheduled.cancel(db_session, entry, actor=admin_user, now=NOW + timedelta(hours=1))

    assert _kinds(sent) == sorted([(admin_user.id, "cancelled"), (normal_user.id, "cancelled")])


def test_a_completed_erasure_tells_the_admins_found_before_the_run(
    db_session, sent, admin_user, normal_user, org
):
    entry = _schedule(db_session, admin_user, normal_user, org)
    sent["frames"].clear()

    assert scheduled._execute_one(db_session, entry.id, NOW + timedelta(hours=73)) is True

    assert _kinds(sent) == sorted([(admin_user.id, "completed"), (normal_user.id, "completed")])


def test_an_incomplete_erasure_sends_no_completion_notice(
    db_session, sent, monkeypatch, admin_user, normal_user, org
):
    monkeypatch.setattr(
        gdpr_erasure_service, "erase_org_member_data", lambda *a, **k: {"complete": False}
    )
    entry = _schedule(db_session, admin_user, normal_user, org)
    sent["frames"].clear()

    scheduled._execute_one(db_session, entry.id, NOW + timedelta(hours=73))

    assert sent["frames"] == []


def test_the_reminder_goes_out_once_inside_the_last_24_hours(
    db_session, sent, admin_user, normal_user, org
):
    _schedule(db_session, admin_user, normal_user, org)
    sent["frames"].clear()

    assert notices.send_due_reminders(db_session, now=NOW + timedelta(hours=40)) == 0
    assert notices.send_due_reminders(db_session, now=NOW + timedelta(hours=49)) == 1
    assert notices.send_due_reminders(db_session, now=NOW + timedelta(hours=50)) == 0

    assert _kinds(sent) == sorted([(admin_user.id, "reminder"), (normal_user.id, "reminder")])


def test_a_short_delay_gets_no_reminder_because_the_request_notice_was_one(
    db_session, sent, admin_user, normal_user, org
):
    _schedule(db_session, admin_user, normal_user, org, delay_hours=12)
    sent["frames"].clear()

    assert notices.send_due_reminders(db_session, now=NOW + timedelta(hours=1)) == 0
    assert sent["frames"] == []


def test_a_notice_that_fails_never_breaks_scheduling(
    db_session, monkeypatch, sent, admin_user, normal_user, org
):
    def boom(*a, **k):
        raise OSError("redis down")

    monkeypatch.setattr("app.utils.websocket_notify.send_ws_event", boom)

    entry = _schedule(db_session, admin_user, normal_user, org)

    assert entry.status == "deferred"


def test_no_mail_is_queued_without_a_transport(
    db_session, monkeypatch, sent, admin_user, normal_user, org
):
    monkeypatch.setattr("app.services.email_service.email_service.has_transport", lambda: False)

    _schedule(db_session, admin_user, normal_user, org)

    assert sent["mails"] == []
    assert sent["frames"]


def test_the_kinds_are_the_four_moments():
    assert {k.value for k in ErasureNoticeKind} == {
        "requested",
        "reminder",
        "cancelled",
        "completed",
    }


def _failing_lookup(db: Session, entry: ErasureLedgerEntry) -> list[tuple[int, str | None]]:
    """A real failed statement: on PostgreSQL it aborts the whole transaction."""
    db.execute(text("SELECT 1 / 0"))
    return []


def test_a_failed_recipient_lookup_leaves_the_transaction_usable(
    db_session, monkeypatch, sent, admin_user, normal_user, org
):
    entry = _schedule(db_session, admin_user, normal_user, org)
    monkeypatch.setattr(notices, "resolve_recipients", _failing_lookup)

    assert notices.notify(db_session, entry, ErasureNoticeKind.REMINDER) == 0

    assert db_session.execute(text("SELECT 1")).scalar() == 1


def test_a_failed_recipient_lookup_never_blocks_the_erasure(
    db_session, monkeypatch, sent, admin_user, normal_user, org
):
    ran: list[int] = []
    monkeypatch.setattr(
        gdpr_erasure_service,
        "erase_org_member_data",
        lambda db, user_id, *a, **k: _record(ran, user_id) and {"complete": True},
    )
    entry = _schedule(db_session, admin_user, normal_user, org)
    monkeypatch.setattr(notices, "resolve_recipients", _failing_lookup)

    assert scheduled._execute_one(db_session, entry.id, NOW + timedelta(hours=73)) is True

    assert ran == [normal_user.id]


def test_the_executor_runs_the_erasure_when_the_notice_lookup_raises(monkeypatch):
    """The same contract without a database: the lookup's error never reaches the run."""
    entry = ErasureLedgerEntry(
        id=1,
        uuid=uuid.uuid4(),
        subject_type="file",
        subject_file_uuid=uuid.uuid4(),
        status="deferred",
        deferred_reason="grace_period",
        scheduled_for=NOW,
        actor_kind="org_admin",
    )
    db = create_autospec(Session, instance=True)
    db.get.return_value = entry
    db.begin_nested.side_effect = contextlib.nullcontext

    def lookup_fails(db: Session, row: ErasureLedgerEntry) -> list[tuple[int, str | None]]:
        raise OperationalError("SELECT", {}, Exception("connection lost"))

    ran: list[ErasureLedgerEntry] = []

    def run(db: Session, row: ErasureLedgerEntry) -> dict[str, Any]:
        ran.append(row)
        return {"complete": True}

    monkeypatch.setattr(scheduled, "_stage", lambda *a, **k: None)
    monkeypatch.setattr(audit_outbox, "deliver_staged", lambda db: None)
    monkeypatch.setattr(notices, "resolve_recipients", lookup_fails)
    monkeypatch.setattr(scheduled, "run_entry", run)

    assert scheduled._execute_one(db, entry.id, NOW + timedelta(hours=1)) is True

    assert ran == [entry]
