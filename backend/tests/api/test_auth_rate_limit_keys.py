"""Sign-in and token refresh are limited per identity, not per shared client address.

Both were limited per client IP only, so every person behind one NAT or office proxy
shared one bucket. Sign-in is now keyed by ``(client IP, normalized username)`` with a
looser per-IP backstop, and refresh by the presented token's verified subject; anything
without an identity stays on the IP.

The test environment disables rate limiting (``RATE_LIMIT_ENABLED=false``); these tests
switch the real limiter on for their own duration and use identities no other test uses,
so their buckets are their own.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import status
from starlette.testclient import TestClient

from app.auth.rate_limit import limiter
from app.auth.token_service import token_service
from app.core.config import settings
from app.core.constants import LOGIN_IP_BACKSTOP_MULTIPLIER

LIMIT = settings.RATE_LIMIT_AUTH_PER_MINUTE


@pytest.fixture
def limiting(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", True)


def _nobody() -> str:
    # Negative sign-ins use accounts that do not exist (never a real account's password).
    return f"nobody-{uuid.uuid4().hex[:12]}@example.com"


def _login(client: TestClient, username: str) -> int:
    response = client.post(
        "/api/auth/token", data={"username": username, "password": "wrong-password"}
    )
    return response.status_code


class TestSignIn:
    def test_one_username_is_limited_but_a_neighbour_behind_the_same_ip_is_not(
        self, client, limiting
    ):
        alice, bob = _nobody(), _nobody()

        within = [_login(client, alice) for _ in range(LIMIT)]
        assert within.count(status.HTTP_429_TOO_MANY_REQUESTS) == 0, within
        assert _login(client, alice) == status.HTTP_429_TOO_MANY_REQUESTS

        assert _login(client, bob) == status.HTTP_401_UNAUTHORIZED

    def test_spellings_of_one_username_share_a_bucket(self, client, limiting):
        alice = _nobody()
        for _ in range(LIMIT):
            _login(client, alice)

        assert _login(client, f"  {alice.upper()} ") == status.HTTP_429_TOO_MANY_REQUESTS

    def test_one_address_still_has_a_ceiling_across_usernames(self, client, limiting, monkeypatch):
        """The per-IP backstop. Measured from a fresh address so earlier tests do not count."""
        address = f"198.51.100.{uuid.uuid4().int % 250 + 1}"
        monkeypatch.setattr("app.utils.client_ip.resolve_client_ip", lambda request: address)
        backstop = LIMIT * LOGIN_IP_BACKSTOP_MULTIPLIER

        statuses = [_login(client, _nobody()) for _ in range(backstop)]
        assert statuses.count(status.HTTP_429_TOO_MANY_REQUESTS) == 0, statuses

        assert _login(client, _nobody()) == status.HTTP_429_TOO_MANY_REQUESTS


def _refresh(client: TestClient, token: str) -> tuple[int, str]:
    """``(status, rotated refresh token or "")``.

    Body token only: a cookie the previous refresh set would also demand a CSRF token.
    """
    client.cookies.clear()
    response = client.post("/api/auth/token/refresh", json={"refresh_token": token})
    rotated: str = response.json().get("refresh_token", "") if response.status_code == 200 else ""
    return response.status_code, rotated


class TestTokenRefresh:
    def test_refresh_is_limited_per_user_not_per_address(
        self, client, db_session, normal_user, admin_user, limiting
    ):
        token, _ = token_service.create_refresh_token(
            db_session, normal_user.id, str(normal_user.uuid), "user"
        )
        other, _ = token_service.create_refresh_token(
            db_session, admin_user.id, str(admin_user.uuid), "admin"
        )

        for _ in range(LIMIT):
            code, token = _refresh(client, token)
            assert code == status.HTTP_200_OK
        assert _refresh(client, token)[0] == status.HTTP_429_TOO_MANY_REQUESTS

        assert _refresh(client, other)[0] == status.HTTP_200_OK

    def test_an_unverifiable_token_is_limited_on_the_address(self, client, limiting, monkeypatch):
        address = f"203.0.113.{uuid.uuid4().int % 250 + 1}"
        monkeypatch.setattr("app.utils.client_ip.resolve_client_ip", lambda request: address)
        forged = [f"forged-{n}" for n in range(LIMIT + 1)]

        statuses = [_refresh(client, t)[0] for t in forged]

        assert statuses[:LIMIT].count(status.HTTP_429_TOO_MANY_REQUESTS) == 0, statuses
        assert statuses[LIMIT] == status.HTTP_429_TOO_MANY_REQUESTS
