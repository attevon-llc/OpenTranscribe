"""FedRAMP AC-2 account-inactivity expiration sweep (#567 item 3).

Drives the real ``run_inactivity_sweep`` against a real Postgres session
(``db_session``) — never a fake DB — since the function's correctness rests
entirely on real filter/count queries (NULL exclusion, the super-admin
protection count) that a hand-mocked query object cannot faithfully exercise.

⚠️ The one test that exercises the sweep's **rollback** path uses
``production_txn_session`` instead, because ``db_session``'s commit and rollback are
asymmetric in a way no application code can be written around. That fixture's
docstring is the full explanation; read it before moving a test between the two.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.auth.audit import AuditEventType
from app.auth.roles import ROLE_SUPER_ADMIN
from app.core.config import settings
from app.db.base import SessionLocal
from app.db.base import engine
from app.models.user import User
from app.services import account_lifecycle_service
from app.services.account_lifecycle_service import run_inactivity_sweep
from tests.db_locks import acquire_ddl_lock_shared


def _user(db_session, *, last_login_at, role="user", is_active=True):
    unique_id = str(uuid.uuid4())[:8]
    user = User(
        email=f"inactivity_{unique_id}@example.com",
        full_name="Inactivity Sweep Test User",
        hashed_password="not-a-real-hash",
        is_active=is_active,
        is_superuser=(role == "super_admin"),
        role=role,
        last_login_at=last_login_at,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def expiration_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ACCOUNT_EXPIRATION_ENABLED", True)
    monkeypatch.setattr(settings, "ACCOUNT_INACTIVE_DAYS", 90)


@pytest.fixture
def audit_events(monkeypatch):
    """Spy on every audit_logger.log call, real object mutation aside."""
    events = []
    monkeypatch.setattr(
        account_lifecycle_service.audit_logger,
        "log",
        lambda **kwargs: events.append(kwargs),
    )
    return events


@pytest.fixture
def production_txn_session():
    """A session whose ``commit()``/``rollback()`` mean what they mean in production.

    **The shared ``db_session`` fixture cannot be used to test a rollback**, and the
    reason is not obvious enough to rediscover a third time.

    ``db_session`` binds a Session to a Connection that already has a *plain*
    transaction open (``connection.begin()``). SQLAlchemy 2.0's default
    ``join_transaction_mode="conditional_savepoint"`` resolves to ``"rollback_only"``
    in that situation — a savepoint is only created when the incoming Connection is
    itself inside one. Under ``rollback_only`` the Session's **root** transaction *is*
    the externally-begun connection transaction, and the two halves are asymmetric:

    * ``session.commit()`` deliberately does **not** commit the outer transaction, so
      test data stays inside it and the fixture can undo everything at teardown; but
    * ``session.rollback()`` always rolls back the **topmost** transaction — which
      here is that same outer transaction. It emits a real ``ROLLBACK``, discarding
      every row the test created, including ones "committed" long before.

    Measured against this fixture: two rows inserted and committed, one more row
    flushed, then ``rollback()`` — and all three vanish, with
    ``connection_transaction.is_active`` flipping to False. The
    ``after_transaction_end`` restart-savepoint listener does not help; it re-creates a
    savepoint *after* the damage, and its condition never fires for the root
    transaction that was actually rolled back. No application code can be written
    around that, because production's ``session_scope()`` has the opposite (and
    correct) asymmetry: ``commit()`` is durable and ``rollback()`` only undoes work
    since the last one.

    So this fixture applies the modern SQLAlchemy 2.0 recipe for joining a session to
    an external transaction — an explicit ``join_transaction_mode="create_savepoint"``
    — on the real production ``SessionLocal``. The session's root transaction becomes a
    SAVEPOINT: ``commit()`` releases it and opens the next one, ``rollback()`` returns
    to it. That is exactly the production contract, while the outer transaction this
    fixture owns is still rolled back at teardown, so nothing is persisted.
    """
    connection = engine.connect()
    transaction = connection.begin()
    # This test opens its own connection, so `db_session`'s advisory lock cannot reach
    # it — take the shared form here or a `ddl_exclusive` test on another xdist worker
    # can deadlock against these `user` writes (see tests/db_locks.py).
    acquire_ddl_lock_shared(connection)
    session = SessionLocal(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        try:
            if transaction.is_active:
                transaction.rollback()
        finally:
            connection.close()


def test_disabled_by_default_touches_nothing(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ACCOUNT_EXPIRATION_ENABLED", False)
    user = _user(db_session, last_login_at=datetime.now(UTC) - timedelta(days=999))

    result = run_inactivity_sweep(db_session)

    assert result == {"status": "disabled", "reason": "not_enabled"}
    db_session.refresh(user)
    assert user.is_active is True


def test_a_user_past_the_threshold_is_deactivated(db_session, expiration_enabled, audit_events):
    stale_login = datetime.now(UTC) - timedelta(days=91)
    user = _user(db_session, last_login_at=stale_login)

    result = run_inactivity_sweep(db_session)

    assert result["deactivated"] == 1
    assert result["candidates_checked"] == 1
    assert result["errors"] == 0
    db_session.refresh(user)
    assert user.is_active is False

    # revoke_all_sessions emits its own AUTH_TOKEN_REVOKE event independently
    # (even with zero real sessions to revoke) — filter to the one this module
    # itself is responsible for.
    expiry_events = [
        e for e in audit_events if e["event_type"] == AuditEventType.AUTH_ACCOUNT_EXPIRED
    ]
    assert len(expiry_events) == 1
    event = expiry_events[0]
    assert event["user_id"] is None, "the sweep has no human actor"
    assert event["target_user_id"] == user.id
    assert event["target_username"] == user.email
    assert event["details"]["trigger"] == "inactivity"
    assert event["details"]["inactive_days_threshold"] == 90


def test_a_user_just_under_the_threshold_stays_active(db_session, expiration_enabled):
    recent_login = datetime.now(UTC) - timedelta(days=89)
    user = _user(db_session, last_login_at=recent_login)

    result = run_inactivity_sweep(db_session)

    assert result["deactivated"] == 0
    assert result["candidates_checked"] == 0
    db_session.refresh(user)
    assert user.is_active is True


def test_null_last_login_at_is_never_treated_as_inactive(db_session, expiration_enabled):
    """NULL means 'never recorded a login', not 'infinitely idle' — see
    models/CLAUDE.md's note on this exact column. An account that has never
    authenticated has nothing to expire.
    """
    user = _user(db_session, last_login_at=None)

    result = run_inactivity_sweep(db_session)

    assert result["deactivated"] == 0
    assert result["candidates_checked"] == 0
    db_session.refresh(user)
    assert user.is_active is True


def test_an_already_inactive_user_is_not_a_candidate(db_session, expiration_enabled):
    stale_login = datetime.now(UTC) - timedelta(days=999)
    user = _user(db_session, last_login_at=stale_login, is_active=False)

    result = run_inactivity_sweep(db_session)

    assert result["candidates_checked"] == 0
    db_session.refresh(user)
    assert user.is_active is False


def _neutralize_other_super_admins(db_session, keep: User) -> None:
    """Demote every OTHER active super_admin in the test DB (e.g. the seeded
    bootstrap account) so a "last super_admin" test controls its own invariant
    instead of silently depending on how many happen to pre-exist — the same
    technique test_last_super_admin_guard.py's TestTheGuardItself uses.
    """
    for other in (
        db_session.query(User)
        .filter(User.role == ROLE_SUPER_ADMIN, User.id != keep.id, User.is_active.is_(True))
        .all()
    ):
        other.role = "user"
        other.is_superuser = False
    db_session.commit()


def test_the_last_active_super_admin_is_skipped_not_deactivated(
    db_session, expiration_enabled, audit_events
):
    stale_login = datetime.now(UTC) - timedelta(days=999)
    lone_super_admin = _user(db_session, last_login_at=stale_login, role="super_admin")
    _neutralize_other_super_admins(db_session, keep=lone_super_admin)

    result = run_inactivity_sweep(db_session)

    assert result["skipped_super_admin"] == 1
    assert result["deactivated"] == 0
    db_session.refresh(lone_super_admin)
    assert lone_super_admin.is_active is True
    assert audit_events == []


def test_an_inactive_super_admin_is_deactivated_when_another_stays_active(
    db_session, expiration_enabled
):
    stale_login = datetime.now(UTC) - timedelta(days=999)
    inactive_admin = _user(db_session, last_login_at=stale_login, role="super_admin")
    # A second, currently-active super_admin — deactivating the first one does
    # not zero out the deployment's admin capability, so it's not protected.
    _user(db_session, last_login_at=datetime.now(UTC), role="super_admin")

    result = run_inactivity_sweep(db_session)

    assert result["deactivated"] == 1
    assert result["skipped_super_admin"] == 0
    db_session.refresh(inactive_admin)
    assert inactive_admin.is_active is False


def test_one_bad_row_does_not_abort_the_whole_sweep(
    production_txn_session, expiration_enabled, monkeypatch
):
    """A failure on one candidate must cost that candidate only.

    Uses ``production_txn_session`` rather than ``db_session`` — see that fixture's
    docstring: under the shared fixture the sweep's ``db.rollback()`` rolls back the
    whole test transaction, so the second candidate's row no longer exists to process
    and the invariant is untestable rather than broken.
    """
    db = production_txn_session
    stale_login = datetime.now(UTC) - timedelta(days=999)
    # Captured as plain ints, not held as ORM objects: a rollback expires every object
    # the session is tracking, so a by-id query is the trustworthy way to read real
    # post-sweep DB state.
    first_id = _user(db, last_login_at=stale_login).id
    second_id = _user(db, last_login_at=stale_login).id

    real_disable = account_lifecycle_service._disable_inactive_user
    failed_ids: list[int] = []

    def flaky_disable(session, user, *, last_login_at):
        if not failed_ids:
            # Whichever candidate the sweep reaches FIRST is the one that fails. The
            # candidate query has no ORDER BY, and pinning the failure to a specific id
            # would let a "stop the batch on the first error" regression pass whenever
            # the healthy row happened to be visited first.
            failed_ids.append(int(user.id))
            # Dirty the session (matches a genuine mid-commit failure) before raising,
            # rather than raising before any DB work happens at all — that's the shape
            # a real failure in here actually takes.
            user.is_active = False
            session.flush()
            raise RuntimeError("simulated failure disabling this one row")
        return real_disable(session, user, last_login_at=last_login_at)

    monkeypatch.setattr(account_lifecycle_service, "_disable_inactive_user", flaky_disable)

    result = run_inactivity_sweep(db)

    # Both rows really were candidates: without this the counts below could be
    # satisfied by ambient stale accounts instead of the two created here.
    assert result["candidates_checked"] == 2
    assert result["errors"] == 1
    assert result["deactivated"] == 1
    assert len(failed_ids) == 1, "exactly one candidate should have been made to fail"

    failed_id = failed_ids[0]
    survivor_id = second_id if failed_id == first_id else first_id
    states = dict(
        db.query(User.id, User.is_active).filter(User.id.in_([first_id, second_id])).all()
    )
    assert set(states) == {first_id, second_id}, "the sweep must not lose either row"
    assert states[survivor_id] is False, (
        "the candidate visited after the failure was skipped — one bad row aborted the batch"
    )
    assert states[failed_id] is True, "the failed candidate's partial write survived the rollback"
