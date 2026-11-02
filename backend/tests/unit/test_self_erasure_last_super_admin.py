"""A due self-service erasure may not remove the last active super administrator (#1354).

The request-time check alone is not enough: two super administrators can both request while each
sees the other, and the other can be demoted or deactivated during the grace period. The check is
repeated when the request is executed.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.core.constants import GDPR_SELF_ERASURE_ENABLED_KEY
from app.core.constants import GDPR_SELF_ERASURE_GRACE_DAYS_KEY
from app.core.security import get_password_hash
from app.models.erasure import ErasureLedgerEntry
from app.models.user import User
from app.services import erasure_ledger_service as ledger
from app.services import self_erasure_service as service
from app.services.system_settings_service import set_setting

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
DUE = NOW + timedelta(days=30)


@pytest.fixture(autouse=True)
def journal_dir(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def enabled(db_session):
    set_setting(db_session, GDPR_SELF_ERASURE_ENABLED_KEY, True)
    set_setting(db_session, GDPR_SELF_ERASURE_GRACE_DAYS_KEY, 14)


@pytest.fixture
def erased(monkeypatch) -> list[int]:
    """Stand-in for the destroy: records the call and leaves the user row in place, which is
    exactly what an erasure that is still running looks like to a concurrent one."""
    calls: list[int] = []

    def fake_erase_user(db, user_id, **kwargs):
        calls.append(int(user_id))
        return {}

    monkeypatch.setattr("app.services.gdpr_erasure_service.erase_user", fake_erase_user)
    return calls


@pytest.fixture
def audit_actions(monkeypatch) -> list[str]:
    actions: list[str] = []
    monkeypatch.setattr(
        "app.services.erasure_ledger_service.audit_logger.log",
        lambda **kw: (
            actions.append(kw["details"]["action"])
            if kw["details"]["action"] == "gdpr_self_erasure_blocked"
            else None
        ),
    )
    return actions


def _super_admin(db) -> User:
    user = User(
        email=f"sa_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name="SA",
        hashed_password=get_password_hash("x"),
        is_active=True,
        is_superuser=True,
        role="super_admin",
    )
    db.add(user)
    db.commit()
    return user


def _entry(db, user) -> ErasureLedgerEntry:
    return service.request_self_erasure(db, user, now=NOW)


def test_two_due_super_admins_erase_exactly_one(
    db_session, super_admin_user, erased, audit_actions
):
    other = _super_admin(db_session)
    first, second = _entry(db_session, super_admin_user), _entry(db_session, other)

    ran = [service._execute_one(db_session, int(e.id), DUE) for e in (first, second)]
    db_session.expire_all()

    assert erased == [super_admin_user.id]
    assert ran == [True, False]
    assert db_session.get(ErasureLedgerEntry, first.id).status == "pending"
    blocked = db_session.get(ErasureLedgerEntry, second.id)
    assert (blocked.status, blocked.deferred_reason) == ("cancelled", None)
    assert audit_actions == ["gdpr_self_erasure_blocked"]


@pytest.mark.parametrize("change", ["deactivated", "demoted"])
def test_super_admin_left_alone_after_the_other_goes_is_not_erased(
    db_session, super_admin_user, erased, audit_actions, change
):
    other = _super_admin(db_session)
    entry = _entry(db_session, super_admin_user)
    if change == "deactivated":
        other.is_active = False
    else:
        other.role = "admin"
        other.is_superuser = False
    db_session.commit()

    assert service._execute_one(db_session, int(entry.id), DUE) is False
    db_session.expire_all()

    assert erased == []
    assert db_session.get(ErasureLedgerEntry, entry.id).status == "cancelled"
    assert db_session.get(User, super_admin_user.id) is not None
    assert audit_actions == ["gdpr_self_erasure_blocked"]


def test_a_super_admin_with_another_active_one_is_erased(
    db_session, super_admin_user, erased, audit_actions
):
    _super_admin(db_session)
    entry = _entry(db_session, super_admin_user)

    assert service._execute_one(db_session, int(entry.id), DUE) is True

    assert erased == [super_admin_user.id]
    assert audit_actions == []


def test_a_non_admin_is_unaffected(db_session, normal_user, erased):
    entry = _entry(db_session, normal_user)

    assert service._execute_one(db_session, int(entry.id), DUE) is True

    assert erased == [normal_user.id]
    assert db_session.get(ErasureLedgerEntry, entry.id).status == "pending"


def test_a_blocked_request_is_not_picked_up_again(db_session, super_admin_user, erased):
    other = _super_admin(db_session)
    entry = _entry(db_session, super_admin_user)
    other.is_active = False
    db_session.commit()
    service._execute_one(db_session, int(entry.id), DUE)

    assert ledger.due_grace_entries(db_session, limit=10, now=DUE) == []
    assert service._execute_one(db_session, int(entry.id), DUE) is False
    assert erased == []
