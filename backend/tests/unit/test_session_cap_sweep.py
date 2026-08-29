"""FedRAMP AC-10 concurrent-session ceiling — the periodic sweep (issue #632).

``services/session_cap_service.py`` is defence in depth for the login-time /
backstop enforcement in ``token_service``: an admin LOWERING the cap does not
retroactively shrink sessions minted under the old, higher one, and a pre-fix
backlog needs a one-time (or nightly, until cleared) catch-up pass. This file
covers the service directly against a real Postgres ``db_session``, and the
Celery task shell's overlap lock against fakes (matching
``test_directory_sync_task.py``'s pattern for the same shape of task).
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services import session_cap_service
from app.services.auth_config_service import AuthConfigService

pytestmark = pytest.mark.xdist_group("auth_config_behaviour")


def _save_session_config(db: Session, user: User, **config) -> None:
    AuthConfigService.bulk_update_category(
        db=db, category="session", config_dict=config, user_id=user.id
    )


def _seed_session(db: Session, user: User) -> RefreshToken:
    now = datetime.now(UTC)
    unique = uuid.uuid4().hex
    row = RefreshToken(
        user_id=user.id,
        token_hash=f"hash-{unique}",
        jti=str(uuid.uuid4()),
        expires_at=now + timedelta(days=7),
        revoked_at=None,
        last_activity_at=now,
        absolute_expires_at=now + timedelta(hours=8),
        user_agent="pytest",
        ip_address="127.0.0.1",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _active_count(db: Session, user: User) -> int:
    return (
        db.query(RefreshToken)
        .filter(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .count()
    )


def _active_jtis(db: Session, user: User) -> set[str]:
    rows = (
        db.query(RefreshToken)
        .filter(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .all()
    )
    return {str(r.jti) for r in rows}


# ── 8. only the offending user is touched ──────────────────────────────────────────


def test_sweep_touches_only_the_user_over_the_limit(
    db_session, normal_user, admin_user, super_admin_user
):
    """Three fresh fixture users, over/at/under a cap of 3.

    This runs against a live, shared dev Postgres (this repo's standard for
    ``db_session``-backed tests — see ``backend/tests/CLAUDE.md``), which can
    already hold real accounts well over any single-digit cap. The sweep's own
    query is deliberately table-wide (every user, not just these three), so the
    report's whole-table aggregates are not asserted here — only the exact JTI
    sets belonging to these three brand-new fixture users, which the sweep must
    get right regardless of what else it also touched.
    """
    _save_session_config(db_session, super_admin_user, max_concurrent_sessions=3)

    over_rows = [_seed_session(db_session, normal_user) for _ in range(5)]  # over
    at_rows = [_seed_session(db_session, admin_user) for _ in range(3)]  # exactly at
    under_rows = [_seed_session(db_session, super_admin_user)]  # under

    report = session_cap_service.run_session_cap_sweep(db_session)

    assert report["status"] == "ok"
    # The over-limit user: exactly the newest 3 (highest id) survive.
    live_over = _active_jtis(db_session, normal_user)
    assert live_over == {str(r.jti) for r in sorted(over_rows, key=lambda r: r.id)[-3:]}
    # The at-limit and under-limit users are untouched, by exact JTI set.
    assert _active_jtis(db_session, admin_user) == {str(r.jti) for r in at_rows}
    assert _active_jtis(db_session, super_admin_user) == {str(r.jti) for r in under_rows}


# ── 9. limit == 0 is unlimited, and touches nothing ─────────────────────────────────


def test_sweep_is_disabled_at_limit_zero(db_session, normal_user):
    _save_session_config(db_session, normal_user, max_concurrent_sessions=0)
    for _ in range(10):
        _seed_session(db_session, normal_user)

    report = session_cap_service.run_session_cap_sweep(db_session)

    assert report == {"status": "disabled", "reason": "unlimited"}
    assert _active_count(db_session, normal_user) == 10


# ── 10. lock contention: the task must not even try ─────────────────────────────────


@contextlib.contextmanager
def _never_acquire(_key, timeout=0, blocking_timeout=0):
    yield False


def test_task_skips_entirely_when_the_lock_is_held(monkeypatch):
    from app.tasks import session_cap as task_mod

    monkeypatch.setattr(task_mod.task_lock_manager, "acquire_lock", _never_acquire, raising=False)

    def _must_not_run(*_a, **_kw):
        pytest.fail("run_session_cap_sweep must not run while the lock is held")

    monkeypatch.setattr(task_mod.session_cap_service, "run_session_cap_sweep", _must_not_run)

    result = task_mod.run_session_cap_sweep_task()

    assert result == {"status": "skipped", "reason": "session cap sweep already running"}


# ── 11. one audit event per offending user ──────────────────────────────────────────


def test_sweep_emits_one_audit_event_per_offending_user(
    db_session, normal_user, super_admin_user, monkeypatch
):
    """One purpose-built audit event for THIS test's offending user.

    Run against the same shared, already-populated dev Postgres as the test
    above — other real accounts may also be over the limit and emit their own
    events, so this asserts exactly one event **naming this test's user**,
    not that the whole run emitted exactly one event.
    """
    events: list[dict] = []
    monkeypatch.setattr(session_cap_service.audit_logger, "log", lambda **kw: events.append(kw))

    _save_session_config(db_session, super_admin_user, max_concurrent_sessions=2)
    for _ in range(4):
        _seed_session(db_session, normal_user)

    session_cap_service.run_session_cap_sweep(db_session)

    own_events = [e for e in events if e.get("target_user_id") == normal_user.id]
    assert len(own_events) == 1, f"expected exactly one event for this user, got {own_events}"
    event = own_events[0]
    assert event["event_type"] == AuditEventType.AUTH_SESSION_LIMIT_EXCEEDED
    assert event["user_id"] is None
    assert event["details"]["reason"] == "periodic_sweep"
    assert event["details"]["sessions_revoked"] == 2
