"""Two concurrent sign-ins cannot both take the last session slot.

The session-cap check locked the user's EXISTING session rows (``SELECT ... FOR
UPDATE``), which cannot stop a row that does not exist yet: two sign-ins one slot
under the cap each counted the same rows and each minted a session. The check now
locks the user's own row first and holds it until the session is minted, so the second
sign-in waits, counts the first one's session and, under the ``reject`` policy, is
refused with 429.

Real PostgreSQL connections through the real endpoint. A rendezvous after the cap check
waits (briefly) for both sign-ins to pass it: before the fix both arrive and both mint;
after it the second is still waiting for the lock, so the first proceeds alone once the
rendezvous times out.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from starlette.testclient import TestClient

import app.api.endpoints.auth.login as login_module
import app.core.auth_settings as auth_settings_module
from app.core.security import get_password_hash
from app.db.base import SessionLocal
from app.main import app
from app.models.refresh_token import RefreshToken
from app.models.user import User

PASSWORD = "Race-condition-pass-1!"


class _Rendezvous:
    """Two callers wait (up to *timeout*) for each other; ``meet`` says whether both came."""

    def __init__(self, timeout: float = 5.0) -> None:
        self._arrived = 0
        self._timeout = timeout
        self._cond = threading.Condition()

    def meet(self) -> bool:
        with self._cond:
            self._arrived += 1
            self._cond.notify_all()
            return self._cond.wait_for(lambda: self._arrived >= 2, timeout=self._timeout)


class _CappedAtOne:
    """The real auth settings, with a one-session cap under the ``reject`` policy."""

    def __init__(self, real) -> None:
        self._real = real

    def __getattr__(self, name: str):
        if name == "max_concurrent_sessions":
            return 1
        if name == "concurrent_session_policy":
            return "reject"
        return getattr(self._real, name)


@pytest.fixture
def capped(monkeypatch):
    real = auth_settings_module.get_auth_settings
    monkeypatch.setattr(login_module, "get_auth_settings", lambda db: _CappedAtOne(real(db)))
    monkeypatch.setattr(
        auth_settings_module, "get_auth_settings", lambda db: _CappedAtOne(real(db))
    )


@pytest.fixture
def committed_user():
    """A local user committed for real (the racers use their own connections)."""
    email = f"session-cap-race-{uuid.uuid4().hex[:12]}@example.com"
    with SessionLocal() as db:
        user = User(
            email=email,
            full_name="Session Cap Race",
            hashed_password=get_password_hash(PASSWORD),
            is_active=True,
            is_superuser=False,
            role="user",
            auth_type="local",
        )
        db.add(user)
        db.commit()
        user_id = int(user.id)
    yield user_id, email
    with SessionLocal() as db:
        db.query(RefreshToken).filter(RefreshToken.user_id == user_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.commit()


def _live_sessions(user_id: int) -> int:
    with SessionLocal() as db:
        return (
            db.query(RefreshToken)
            .filter(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .count()
        )


def test_two_concurrent_sign_ins_one_slot_under_the_cap(committed_user, capped, monkeypatch):
    user_id, email = committed_user
    rendezvous = _Rendezvous()
    met: list[bool] = []
    real_mfa_check = login_module._check_mfa_requirement

    def _after_the_cap_check(*args, **kwargs):
        met.append(rendezvous.meet())
        return real_mfa_check(*args, **kwargs)

    monkeypatch.setattr(login_module, "_check_mfa_requirement", _after_the_cap_check)
    statuses: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        # No ``with``: entering the client would run the app's lifespan.
        response = TestClient(app).post(
            "/api/auth/token", data={"username": email, "password": PASSWORD}
        )
        with lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(statuses) == [200, 429]
    assert _live_sessions(user_id) == 1
    # Only one sign-in got past the cap check while the other held the lock.
    assert met == [False]
