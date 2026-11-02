"""The reconcile sweep and a self-service request's grace period (#1354)."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.core.constants import GDPR_SELF_ERASURE_ENABLED_KEY
from app.services import erasure_ledger_service as ledger
from app.services import gdpr_erasure_service
from app.services import self_erasure_service as service
from app.services.system_settings_service import set_setting

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def journal_dir(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)


@pytest.fixture()
def request_entry(db_session, normal_user):
    set_setting(db_session, GDPR_SELF_ERASURE_ENABLED_KEY, True)
    return service.request_self_erasure(db_session, normal_user, now=NOW)


@pytest.fixture()
def erase_calls(monkeypatch):
    calls: list[int] = []

    def fake_erase_user(db, user_id, **kwargs):
        calls.append(user_id)
        return {"complete": True}

    monkeypatch.setattr(gdpr_erasure_service, "erase_user", fake_erase_user)
    return calls


def test_reconcile_does_not_erase_during_grace(db_session, request_entry, erase_calls):
    before_due = NOW + timedelta(days=13)
    assert ledger.due_grace_entries(db_session, limit=10, now=before_due) == []
    assert service._execute_one(db_session, request_entry.id, before_due) is False
    assert erase_calls == []
    db_session.refresh(request_entry)
    assert request_entry.status == "deferred"

    due = NOW + timedelta(days=14)
    assert [e.id for e in ledger.due_grace_entries(db_session, limit=10, now=due)] == [
        request_entry.id
    ]
    assert service._execute_one(db_session, request_entry.id, due) is True
    assert erase_calls == [request_entry.subject_user_id]
    db_session.refresh(request_entry)
    assert request_entry.status == "pending"
    assert request_entry.deferred_reason is None


def test_open_entries_never_retries_a_grace_request_as_an_ordinary_deferral(
    db_session, request_entry
):
    ids = {e.id for e in ledger.open_entries(db_session, limit=100)}
    assert request_entry.id not in ids


def test_cancelled_entry_never_executes_and_is_not_overdue(
    db_session, normal_user, request_entry, erase_calls
):
    service.cancel_self_erasure(db_session, normal_user, request_entry.uuid, now=NOW)
    request_entry.sla_due_at = NOW - timedelta(days=1)
    db_session.commit()

    late = NOW + timedelta(days=60)
    assert service._execute_one(db_session, request_entry.id, late) is False
    assert erase_calls == []
    assert request_entry.id not in {e.id for e in ledger.open_entries(db_session, limit=100)}
    assert request_entry.id not in {e.id for e in ledger.overdue_entries(db_session, now=late)}


def test_an_ordinary_deferral_is_still_retried_and_can_be_overdue(db_session, normal_user):
    entry = ledger.record_request(
        db_session,
        subject_type="user",
        subject_user_id=normal_user.id,
        subject_user_uuid=normal_user.uuid,
        actor_kind="super_admin",
    )
    assert entry is not None
    entry.status = "deferred"
    entry.deferred_reason = "legal_hold"
    entry.sla_due_at = NOW - timedelta(days=1)
    db_session.commit()

    assert entry.id in {e.id for e in ledger.open_entries(db_session, limit=100)}
    assert entry.id in {e.id for e in ledger.overdue_entries(db_session, now=NOW)}
