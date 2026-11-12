"""Removing a member schedules the erase of their personal files after a grace period (#1104, OD-4).

Real routes, real database and the real scheduled-erasure executor; only the websocket and mail
channels are recorded. The organization-content hand-over is unchanged (``test_org_content_transfer``).
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from unittest.mock import create_autospec

import pytest
from sqlalchemy.orm import Session

from app.core.constants import GDPR_REMOVED_MEMBER_GRACE_DAYS_KEY
from app.models.erasure import ErasureLedgerEntry
from app.models.media import MediaFile
from app.models.user import User
from app.services import audit_outbox
from app.services import removed_member_service as removal
from app.services import scheduled_erasure_service as scheduled
from app.services import user_deletion_service
from app.services.permission_service import PermissionService
from app.services.system_settings_service import set_setting


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(audit_outbox, "deliver_staged", lambda db: None)
    monkeypatch.setattr(
        user_deletion_service.token_service, "stamp_user_revocation_epoch", lambda uuid: None
    )
    monkeypatch.setattr("app.services.account_purge.purge_account_external_copies", lambda plan: [])
    monkeypatch.setattr("app.core.celery.celery_app.send_task", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def frames(monkeypatch) -> list[tuple[int, str]]:
    seen: list[tuple[int, str]] = []

    def record(user_id: int, event: str, data: dict) -> bool:
        seen.append((user_id, data["event"]))
        return True

    monkeypatch.setattr("app.utils.websocket_notify.send_ws_event", record)
    monkeypatch.setattr("app.services.email_service.email_service.has_transport", lambda: False)
    return seen


def _entry(db, user) -> ErasureLedgerEntry:
    entry: ErasureLedgerEntry | None = (
        db.query(ErasureLedgerEntry)
        .filter(ErasureLedgerEntry.subject_user_id == user.id)
        .order_by(ErasureLedgerEntry.id.desc())
        .first()
    )
    assert entry is not None
    return entry


def _due(entry: ErasureLedgerEntry) -> datetime:
    assert entry.scheduled_for is not None
    return entry.scheduled_for


def _wait(entry: ErasureLedgerEntry) -> timedelta:
    return _due(entry) - entry.requested_at


def test_removing_a_member_schedules_the_erase_and_keeps_the_personal_files(gov, frames):
    personal = gov.file(gov.alice, org=False)
    org_file = gov.file(gov.alice)
    personal_id, org_file_id = personal.id, org_file.id

    result = removal.remove_account(gov.db, gov.alice, gov.admin)

    assert result.scheduled
    entry = _entry(gov.db, gov.alice)
    assert (entry.status, entry.deferred_reason, entry.actor_kind) == (
        "deferred",
        "grace_period",
        "org_admin",
    )
    assert timedelta(days=13, hours=23) < _wait(entry) <= timedelta(days=14)
    gov.db.expire_all()
    assert gov.db.get(User, gov.alice.id).is_active is False
    assert gov.db.get(MediaFile, personal_id) is not None
    assert gov.db.get(MediaFile, org_file_id).user_id == gov.admin.id
    assert (gov.admin.id, "requested") in frames


def test_the_grace_period_is_a_setting_and_zero_erases_at_once(gov):
    set_setting(gov.db, GDPR_REMOVED_MEMBER_GRACE_DAYS_KEY, 3)
    gov.file(gov.alice, org=False)
    removal.remove_account(gov.db, gov.alice, gov.admin)
    entry = _entry(gov.db, gov.alice)
    assert _wait(entry) <= timedelta(days=3)

    set_setting(gov.db, GDPR_REMOVED_MEMBER_GRACE_DAYS_KEY, 0)
    personal = gov.file(gov.bob, org=False)
    personal_id = personal.id
    result = removal.remove_account(gov.db, gov.bob, gov.admin)
    assert not result.scheduled
    gov.db.expire_all()
    assert gov.db.get(MediaFile, personal_id) is None


def test_an_account_in_no_organization_is_still_erased_at_once(gov):
    personal = gov.file(gov.outsider, org=False)
    personal_id = personal.id

    result = removal.remove_account(gov.db, gov.outsider, gov.admin)

    assert not result.scheduled
    gov.db.expire_all()
    assert gov.db.get(MediaFile, personal_id) is None


def test_restore_cancels_the_erase_and_reactivates_the_account(gov, frames):
    personal = gov.file(gov.alice, org=False)
    personal_id = personal.id
    removal.remove_account(gov.db, gov.alice, gov.admin)
    entry = _entry(gov.db, gov.alice)

    removal.restore_account(gov.db, entry, gov.admin)

    gov.db.expire_all()
    assert _entry(gov.db, gov.alice).status == "cancelled"
    assert gov.db.get(User, gov.alice.id).is_active is True
    assert gov.db.get(MediaFile, personal_id) is not None
    assert (gov.admin.id, "cancelled") in frames
    assert removal.open_removals(gov.db) == []


def test_restore_after_the_grace_is_refused(gov):
    removal.remove_account(gov.db, gov.alice, gov.admin)
    entry = _entry(gov.db, gov.alice)
    later = _due(entry) + timedelta(minutes=1)

    with pytest.raises(scheduled.ScheduledErasureError) as refused:
        removal.restore_account(gov.db, entry, gov.admin, now=later)

    assert refused.value.code == "already_executing"
    gov.db.expire_all()
    assert gov.db.get(User, gov.alice.id).is_active is False


@pytest.mark.usefixtures("no_receipt_measurement", "no_erasure_extras")
def test_expiry_erases_the_personal_files_and_a_held_file_is_skipped(gov, frames):
    personal = gov.file(gov.alice, org=False)
    held = gov.file(gov.alice, org=False)
    personal_id, held_id = personal.id, held.id
    removal.remove_account(gov.db, gov.alice, gov.admin)
    held.legal_hold = True
    held.is_quarantined = True
    gov.db.commit()
    entry = _entry(gov.db, gov.alice)
    due = _due(entry) + timedelta(minutes=1)
    frames.clear()

    assert scheduled._execute_one(gov.db, entry.id, due) is True

    gov.db.expire_all()
    assert gov.db.get(MediaFile, personal_id) is None
    assert gov.db.get(MediaFile, held_id) is not None
    assert (gov.admin.id, "completed") not in frames  # a held file keeps it incomplete


def test_expiry_runs_the_account_erasure_and_reports_completion(gov, frames, monkeypatch):
    from app.services import gdpr_erasure_service

    ran: list[int] = []

    def fake_erase_user(db, user_id: int, **kwargs) -> dict:
        ran.append(user_id)
        return {"complete": True}

    monkeypatch.setattr(gdpr_erasure_service, "erase_user", fake_erase_user)
    removal.remove_account(gov.db, gov.alice, gov.admin)
    entry = _entry(gov.db, gov.alice)
    assert ran == []  # nothing runs at request time
    frames.clear()

    assert scheduled._execute_one(gov.db, entry.id, _due(entry) + timedelta(minutes=1))

    assert ran == [gov.alice.id]
    assert (gov.admin.id, "completed") in frames


def test_others_lose_access_through_shares_while_the_erase_is_pending(gov):
    shared = gov.file(gov.alice, org=False)
    collection = gov.collection(gov.alice, org=False)
    gov.put(collection, shared)
    gov.share(collection, gov.bob)
    assert PermissionService.get_file_permission(gov.db, shared.id, gov.bob.id) == "viewer"

    removal.remove_account(gov.db, gov.alice, gov.admin)

    assert PermissionService.get_file_permission(gov.db, shared.id, gov.bob.id) is None
    ids = PermissionService.get_accessible_file_ids_subquery(gov.db, gov.bob.id)
    assert shared.id not in {r[0] for r in gov.db.execute(ids.select())}


def test_the_reminder_goes_out_for_a_removal(gov, frames, monkeypatch):
    from app.services import erasure_notifications as notices

    class _Redis:
        def set(self, *a, **k):
            return True

    monkeypatch.setattr(notices, "get_redis", lambda: _Redis())
    removal.remove_account(gov.db, gov.alice, gov.admin)
    entry = _entry(gov.db, gov.alice)
    frames.clear()

    sent = notices.send_due_reminders(gov.db, now=_due(entry) - timedelta(hours=2))

    assert sent == 1
    assert (gov.admin.id, "reminder") in frames


# --- Who may restore, and the restore's atomicity ------------------------------------------------

RESTORE_URL = "/api/admin/removed-members/{}/restore"
FUTURE = datetime(2100, 1, 1, tzinfo=UTC)


def _session_holding(subject: User) -> Any:
    """A session double that finds one account, the only read the restore route's guard makes."""
    db = create_autospec(Session, instance=True)
    db.get.side_effect = lambda model, ident: subject if ident == subject.id else None
    return db


def _removal_of(subject: User, removed_by: str) -> ErasureLedgerEntry:
    return ErasureLedgerEntry(
        uuid=uuid.uuid4(),
        subject_type="user",
        subject_user_id=subject.id,
        subject_user_uuid=uuid.uuid4(),
        actor_kind=removed_by,
        requested_at=datetime(2099, 12, 1, tzinfo=UTC),
        scheduled_for=FUTURE,
        status="deferred",
        deferred_reason="grace_period",
    )


@pytest.mark.parametrize(
    ("subject_role", "removed_by", "caller_role", "allowed"),
    [
        ("super_admin", "org_admin", "admin", False),
        ("user", "super_admin", "admin", False),
        ("super_admin", "org_admin", "super_admin", True),
        ("user", "super_admin", "super_admin", True),
        ("user", "org_admin", "admin", True),
    ],
)
def test_only_a_super_admin_restores_a_super_admin_or_undoes_a_super_admins_removal(
    monkeypatch, subject_role, removed_by, caller_role, allowed
):
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.api.endpoints import admin_removed_members as route
    from app.auth import refusal_audit

    subject = User(id=7, role=subject_role, email="subject@example.com")
    caller = User(id=8, role=caller_role, email="caller@example.com")
    entry = _removal_of(subject, removed_by)
    restored: list[User] = []
    refusals: list[str] = []

    def restore(db: Session, e: ErasureLedgerEntry, actor: User, client: Any = None) -> Any:
        restored.append(actor)
        return e

    def audit(**event: Any) -> None:
        refusals.append(event["details"]["action"])

    monkeypatch.setattr(route.removal, "find_removal", lambda db, ledger_uuid: entry)
    monkeypatch.setattr(route.removal, "restore_account", restore)
    monkeypatch.setattr(refusal_audit.audit_logger, "log", audit)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    db = _session_holding(subject)

    if allowed:
        route.restore_removed_member(request, entry.uuid, db, caller)
        assert restored == [caller]
        assert refusals == []
    else:
        with pytest.raises(HTTPException) as refused:
            route.restore_removed_member(request, entry.uuid, db, caller)
        assert refused.value.status_code == 403
        assert restored == []
        assert len(refusals) == 1


def test_a_user_cannot_restore_and_an_anonymous_caller_is_refused(gov, client, user_token_headers):
    removal.remove_account(gov.db, gov.alice, gov.admin)
    url = RESTORE_URL.format(_entry(gov.db, gov.alice).uuid)

    assert client.post(url, headers=user_token_headers).status_code == 403
    assert client.post(url).status_code == 401
    gov.db.expire_all()
    assert _entry(gov.db, gov.alice).status == "deferred"


def test_over_http_a_plain_admin_cannot_restore_a_super_admin_and_a_super_admin_can(
    gov, client, admin_token_headers, super_admin_token_headers
):
    gov.alice.role = "super_admin"
    gov.alice.is_superuser = True
    gov.db.commit()
    removal.remove_account(gov.db, gov.alice, gov.admin)
    url = RESTORE_URL.format(_entry(gov.db, gov.alice).uuid)

    assert client.post(url, headers=admin_token_headers).status_code == 403
    gov.db.expire_all()
    assert _entry(gov.db, gov.alice).status == "deferred"
    assert gov.db.get(User, gov.alice.id).is_active is False

    assert client.post(url, headers=super_admin_token_headers).status_code == 200
    gov.db.expire_all()
    assert gov.db.get(User, gov.alice.id).is_active is True


def test_restore_rereads_the_entry_under_the_executors_row_lock(monkeypatch):
    """The executor flips ``deferred`` to ``pending`` under ``FOR UPDATE``; a cancel that checks
    a copy read before that commit must see ``pending`` once it holds the lock."""
    entry = _removal_of(User(id=7, role="user", email="subject@example.com"), "org_admin")
    locks: list[object] = []
    cancelled: list[ErasureLedgerEntry] = []

    def executor_won_the_lock(row: ErasureLedgerEntry, with_for_update: object = None) -> None:
        locks.append(with_for_update)
        row.status, row.deferred_reason = "pending", None

    def cancel_request(db: Session, e: ErasureLedgerEntry, now: Any = None) -> Any:
        cancelled.append(e)
        return e

    db = create_autospec(Session, instance=True)
    db.refresh.side_effect = executor_won_the_lock
    monkeypatch.setattr(scheduled, "_stage", lambda *a, **k: None)
    monkeypatch.setattr(scheduled.notices, "notify", lambda *a, **k: 0)
    monkeypatch.setattr(scheduled.ledger, "cancel_request", cancel_request)

    with pytest.raises(scheduled.ScheduledErasureError) as refused:
        scheduled.cancel(
            db,
            entry,
            actor=User(id=1, role="admin", email="admin@example.com"),
            now=datetime(2099, 1, 1, tzinfo=UTC),
        )

    assert refused.value.code == "already_executing"
    assert locks == [True]
    assert cancelled == []


def test_the_account_is_switched_off_in_the_scheduled_erases_own_commit(gov, monkeypatch):
    def notice_fails(*a, **k):
        raise RuntimeError("after the ledger commit")

    monkeypatch.setattr(scheduled.notices, "notify", notice_fails)

    with pytest.raises(RuntimeError):
        removal.remove_account(gov.db, gov.alice, gov.admin)

    gov.db.rollback()
    assert _entry(gov.db, gov.alice).status == "deferred"
    assert gov.db.get(User, gov.alice.id).is_active is False


def test_restore_switches_the_account_on_in_the_cancellations_own_commit(gov, monkeypatch):
    removal.remove_account(gov.db, gov.alice, gov.admin)
    entry = _entry(gov.db, gov.alice)

    def notice_fails(*a, **k):
        raise RuntimeError("after the cancellation commit")

    monkeypatch.setattr(scheduled.notices, "notify", notice_fails)

    with pytest.raises(RuntimeError):
        removal.restore_account(gov.db, entry, gov.admin)

    gov.db.rollback()
    assert _entry(gov.db, gov.alice).status == "cancelled"
    assert gov.db.get(User, gov.alice.id).is_active is True


def test_restore_leaves_an_account_that_was_already_off_switched_off(gov):
    gov.alice.is_active = False
    gov.db.commit()
    removal.remove_account(gov.db, gov.alice, gov.admin)

    removal.restore_account(gov.db, _entry(gov.db, gov.alice), gov.admin)

    gov.db.expire_all()
    assert _entry(gov.db, gov.alice).status == "cancelled"
    assert gov.db.get(User, gov.alice.id).is_active is False
