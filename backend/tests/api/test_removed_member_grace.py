"""Removing a member schedules the erase of their personal files after a grace period (#1104, OD-4).

Real routes, real database and the real scheduled-erasure executor; only the websocket and mail
channels are recorded. The organization-content hand-over is unchanged (``test_org_content_transfer``).
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta

import pytest

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
