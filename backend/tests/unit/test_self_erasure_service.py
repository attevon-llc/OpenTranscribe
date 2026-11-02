"""Self-service erasure request: creation, cancellation, refusals, journal round trip (#1354)."""

from __future__ import annotations

import hashlib
import json
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.core.constants import GDPR_SELF_ERASURE_ENABLED_KEY
from app.core.constants import GDPR_SELF_ERASURE_GRACE_DAYS_KEY
from app.models.erasure import ErasureLedgerEntry
from app.services import erasure_ledger_service as ledger
from app.services import self_erasure_service as service
from app.services.system_settings_service import set_setting
from tests.audit_maintenance import audit_maintenance

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def journal_dir(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def enabled(db_session):
    set_setting(db_session, GDPR_SELF_ERASURE_ENABLED_KEY, True)
    set_setting(db_session, GDPR_SELF_ERASURE_GRACE_DAYS_KEY, 14)


def test_request_creates_deferred_ledger_entry_with_schedule(db_session, normal_user, enabled):
    entry = service.request_self_erasure(db_session, normal_user, now=NOW)

    assert entry.status == "deferred"
    assert entry.deferred_reason == "grace_period"
    assert entry.actor_kind == "data_subject"
    assert entry.scheduled_for == NOW + timedelta(days=14)
    assert entry.requested_at == NOW
    assert entry.subject_user_id == normal_user.id
    assert entry.subject_user_uuid == normal_user.uuid
    canonical = json.dumps(
        {
            "ledger_uuid": str(entry.uuid),
            "requested_at": NOW.isoformat(),
            "scheduled_for": (NOW + timedelta(days=14)).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert entry.request_digest == hashlib.sha256(canonical.encode()).hexdigest()


def test_disabled_by_default(db_session, normal_user):
    with pytest.raises(service.SelfErasureError) as caught:
        service.request_self_erasure(db_session, normal_user, now=NOW)
    assert caught.value.code == "disabled"
    assert db_session.query(ErasureLedgerEntry).count() == 0


@pytest.mark.parametrize(("configured", "expected"), [(60, 21), (-3, 0), (7, 7)])
def test_grace_days_clamped_to_21(db_session, configured, expected):
    set_setting(db_session, GDPR_SELF_ERASURE_GRACE_DAYS_KEY, configured)
    assert service.grace_days(db_session) == expected


def test_a_second_request_returns_the_existing_one(db_session, normal_user, enabled):
    first = service.request_self_erasure(db_session, normal_user, now=NOW)
    with pytest.raises(service.SelfErasureError) as caught:
        service.request_self_erasure(db_session, normal_user, now=NOW + timedelta(days=1))
    assert caught.value.code == "request_exists"
    assert caught.value.entry is not None
    assert caught.value.entry.uuid == first.uuid


def test_the_last_super_admin_is_refused(db_session, super_admin_user, enabled):
    with pytest.raises(service.SelfErasureError) as caught:
        service.request_self_erasure(db_session, super_admin_user, now=NOW)
    assert caught.value.code == "last_super_admin"
    assert db_session.query(ErasureLedgerEntry).count() == 0


def test_cancel_before_the_schedule_marks_cancelled(db_session, normal_user, enabled):
    entry = service.request_self_erasure(db_session, normal_user, now=NOW)
    cancelled = service.cancel_self_erasure(
        db_session, normal_user, entry.uuid, now=NOW + timedelta(days=1)
    )
    assert cancelled.status == "cancelled"
    assert cancelled.cancelled_at == NOW + timedelta(days=1)
    assert service.get_open_request(db_session, normal_user.id) is None


def test_cancel_after_the_schedule_is_refused(db_session, normal_user, enabled):
    entry = service.request_self_erasure(db_session, normal_user, now=NOW)
    with pytest.raises(service.SelfErasureError) as caught:
        service.cancel_self_erasure(
            db_session, normal_user, entry.uuid, now=NOW + timedelta(days=14)
        )
    assert caught.value.code == "already_executing"
    db_session.refresh(entry)
    assert entry.status == "deferred"


def test_cancelling_another_users_request_is_not_found(
    db_session, normal_user, other_user, enabled
):
    entry = service.request_self_erasure(db_session, normal_user, now=NOW)
    with pytest.raises(service.SelfErasureError) as caught:
        service.cancel_self_erasure(db_session, other_user, entry.uuid, now=NOW)
    assert caught.value.code == "not_found"
    db_session.refresh(entry)
    assert entry.status == "deferred"


def test_db_restore_reopens_request_as_deferred(db_session, normal_user, enabled):
    entry = service.request_self_erasure(db_session, normal_user, now=NOW)
    original = (entry.uuid, entry.scheduled_for, entry.request_digest)
    # Simulate losing the row the way a restore would: only the owner may, under maintenance.
    with audit_maintenance(db_session):
        db_session.delete(entry)
        db_session.flush()
    db_session.commit()

    assert ledger.restore_from_journal(db_session) == 1

    restored = db_session.query(ErasureLedgerEntry).filter_by(uuid=original[0]).one()
    assert restored.status == "deferred"
    assert restored.deferred_reason == "grace_period"
    assert restored.scheduled_for == original[1]
    assert restored.request_digest == original[2]


def test_db_restore_does_not_revive_a_cancelled_request(db_session, normal_user, enabled):
    entry = service.request_self_erasure(db_session, normal_user, now=NOW)
    service.cancel_self_erasure(db_session, normal_user, entry.uuid, now=NOW)
    entry_uuid = entry.uuid
    # Simulate losing the row the way a restore would: only the owner may, under maintenance.
    with audit_maintenance(db_session):
        db_session.delete(entry)
        db_session.flush()
    db_session.commit()

    ledger.restore_from_journal(db_session)

    restored = db_session.query(ErasureLedgerEntry).filter_by(uuid=entry_uuid).one()
    assert restored.status == "cancelled"
    assert restored.cancelled_at is not None


def test_an_ordinary_entry_still_restores_as_pending(db_session, normal_user):
    entry = ledger.record_request(
        db_session,
        subject_type="user",
        subject_user_id=normal_user.id,
        subject_user_uuid=normal_user.uuid,
        actor_kind="super_admin",
    )
    assert entry is not None
    entry_uuid = entry.uuid
    # Simulate losing the row the way a restore would: only the owner may, under maintenance.
    with audit_maintenance(db_session):
        db_session.delete(entry)
        db_session.flush()
    db_session.commit()

    ledger.restore_from_journal(db_session)

    restored = db_session.query(ErasureLedgerEntry).filter_by(uuid=entry_uuid).one()
    assert restored.status == "pending"
    assert restored.deferred_reason is None
    assert isinstance(restored.uuid, uuid_pkg.UUID)
