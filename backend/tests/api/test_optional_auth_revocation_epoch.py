"""``get_optional_current_user`` honours the per-user revocation epoch.

Access tokens are stateless, so the per-user epoch (stamped by logout-all, a password
reset, an admin session termination, a role change) is the only thing that kills one that
is still inside its lifetime. ``get_current_user`` feeds each token's ``iat`` into the
check; the optional-auth sibling did not, so a token the user had explicitly revoked still
resolved to the user on every optional-auth path (``require_capability``'s platform bypass,
the file routes that serve public and private media, and the capabilities endpoint).

``tests/unit/test_access_token_revocation_epoch.py`` pins the strict dependency. This file
pins the same contract on the optional one, against a real user row and a real in-memory
revocation store, plus the control cases that stop a "never accept anything" fix passing.
"""

from __future__ import annotations

import time
import uuid as uuid_pkg
from typing import Any

import pytest
from starlette.requests import Request

from app.api.endpoints.auth import dependencies as deps_module
from app.api.endpoints.auth.dependencies import get_optional_current_user
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.session import InMemoryStore
from app.auth.token_service import USER_REVOCATION_EPOCH_PREFIX
from app.auth.token_service import TokenService
from app.core.config import settings
from app.core.security import get_password_hash
from app.models.user import User
from tests.jwt_compat import jwt


@pytest.fixture
def revocation(monkeypatch) -> TokenService:
    """A ``TokenService`` on a private in-memory store, wired into the dependency."""
    service = TokenService()
    service._store = InMemoryStore()
    monkeypatch.setattr(deps_module, "token_service", service)
    monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", True)
    return service


@pytest.fixture
def account(db_session) -> User:
    unique = uuid_pkg.uuid4().hex[:8]
    user = User(
        email=f"optional-epoch-{unique}@example.com",
        full_name="Epoch Person",
        hashed_password=get_password_hash("irrelevant-Passphrase99!"),
        role="user",
        auth_type="local",
        is_active=True,
        is_superuser=False,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _token(user: User, *, issued_at: int) -> str:
    """An otherwise-valid access token with a chosen ``iat``.

    Minted with the independent test encoder because ``direct_auth.create_access_token``
    stamps ``iat`` itself, and the claim under test has to be the test's choice.
    """
    return jwt.encode(
        {
            "sub": str(user.uuid),
            "type": TOKEN_TYPE_ACCESS,
            "jti": uuid_pkg.uuid4().hex,
            "iat": issued_at,
            "exp": int(time.time()) + 600,
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def _request(token: str) -> Any:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "server": ("backend", 8080),
            "root_path": "",
            "path": "/api/system/capabilities",
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "client": ("10.0.0.7", 40000),
        }
    )


def _stamp_epoch(service: TokenService, user: User, *, seconds_ago: int) -> int:
    epoch = int(time.time()) - seconds_ago
    service.store.set(f"{USER_REVOCATION_EPOCH_PREFIX}{user.uuid}", str(epoch), ex=600)
    return epoch


def test_optional_user_rejects_token_issued_before_revocation_epoch(
    db_session, revocation, account
):
    """Logout-all must also end the session on optional-auth routes."""
    epoch = _stamp_epoch(revocation, account, seconds_ago=0)
    stale = _token(account, issued_at=epoch - 60)

    assert get_optional_current_user(_request(stale), db_session) is None


def test_optional_user_accepts_token_issued_after_revocation_epoch(db_session, revocation, account):
    """Control: signing in again after the revocation works at once."""
    _stamp_epoch(revocation, account, seconds_ago=120)
    fresh = _token(account, issued_at=int(time.time()))

    user = get_optional_current_user(_request(fresh), db_session)

    assert user is not None
    assert user.id == account.id


def test_optional_user_accepts_old_token_when_no_epoch_was_stamped(db_session, revocation, account):
    """Control: the epoch bites only when something stamped it."""
    old = _token(account, issued_at=int(time.time()) - 3600)

    user = get_optional_current_user(_request(old), db_session)

    assert user is not None
    assert user.id == account.id


def test_another_users_epoch_does_not_refuse_this_token(db_session, revocation, account):
    """Control: the epoch is per user, not deployment-wide."""
    revocation.stamp_user_revocation_epoch("019ec90a-1b2c-7def-8000-0000000000dd")
    old = _token(account, issued_at=int(time.time()) - 3600)

    user = get_optional_current_user(_request(old), db_session)

    assert user is not None
    assert user.id == account.id
