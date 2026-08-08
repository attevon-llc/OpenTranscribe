"""JWT ``type`` claim binding — a token is only valid for its own purpose.

Every token this app mints is signed with the same ``JWT_SECRET_KEY``, so the
``type`` claim is the only thing separating them. Before this was enforced,
``get_current_user`` checked ``sub``/``jti``/revocation but never ``type``, which
made the **MFA half-token a full access token**: it is handed to a client in the
login response body *before* the second factor is verified, so presenting it as
``Authorization: Bearer ...`` bypassed MFA outright. Refresh tokens were saved
only by an algorithm accident (HS512 vs HS256), and ``core.security.verify_token``
accepts both in non-FIPS mode — so a refresh token already opened a WebSocket.

These tests pin the rejection at every consumer.
"""

# mypy: disable-error-code="arg-type,no-any-return"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures that declare Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object. Declared once here rather than as a cast at every call site — casts
# bury the assertion, and widening a production signature to suit a test is worse.
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.endpoints.auth.dependencies import get_current_user
from app.api.endpoints.auth.dependencies import get_optional_current_user
from app.api.endpoints.auth.mfa_tokens import _create_mfa_token
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.constants import TOKEN_TYPE_MFA
from app.auth.constants import TOKEN_TYPE_REFRESH
from app.auth.direct_auth import create_access_token
from app.core.security import verify_token

USER_UUID = "019ec90a-1b2c-7def-8000-0000000000ff"


def _request(token: str | None = None) -> SimpleNamespace:
    """A Request stand-in: the type check runs before anything touches it.

    ``get_optional_current_user`` reads the bearer token off the request itself
    rather than taking it as a parameter, so the header goes here.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(state=SimpleNamespace(), cookies={}, headers=headers)


@pytest.fixture
def mfa_token() -> str:
    return _create_mfa_token(USER_UUID, "user")


@pytest.fixture
def refresh_token() -> str:
    """A refresh token, minted the way token_service mints one."""
    import uuid
    from datetime import UTC
    from datetime import datetime
    from datetime import timedelta

    from app.core.config import settings
    from tests.jwt_compat import jwt

    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": USER_UUID,
            "role": "user",
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + timedelta(days=7),
            "type": TOKEN_TYPE_REFRESH,
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


@pytest.fixture
def access_token() -> str:
    return create_access_token({"sub": USER_UUID, "role": "user"})


def _decode(token: str) -> dict:
    from app.core.config import settings
    from tests.jwt_compat import jwt

    return jwt.decode(
        token,
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM, settings.JWT_ALGORITHM_V3],
        options={"verify_aud": False},
    )


class TestTokensCarryTheirType:
    def test_access_token_is_typed(self, access_token):
        assert _decode(access_token)["type"] == TOKEN_TYPE_ACCESS

    def test_mfa_token_is_typed(self, mfa_token):
        assert _decode(mfa_token)["type"] == TOKEN_TYPE_MFA

    def test_core_security_access_token_is_typed(self):
        from app.core.security import create_access_token as core_create

        assert _decode(core_create(USER_UUID))["type"] == TOKEN_TYPE_ACCESS


class TestGetCurrentUserRejectsForeignTypes:
    """The MFA-bypass regression. Rejection happens before any DB access."""

    def test_mfa_token_rejected(self, mfa_token):
        with pytest.raises(HTTPException) as exc:
            get_current_user(request=_request(), token=mfa_token, db=None)
        assert exc.value.status_code == 401

    def test_refresh_token_rejected(self, refresh_token):
        with pytest.raises(HTTPException) as exc:
            get_current_user(request=_request(), token=refresh_token, db=None)
        assert exc.value.status_code == 401

    def test_untyped_legacy_token_rejected(self):
        """A token minted before purpose binding has no type and is not trusted."""
        import uuid
        from datetime import UTC
        from datetime import datetime
        from datetime import timedelta

        from app.core.config import settings
        from tests.jwt_compat import jwt

        now = datetime.now(UTC)
        untyped = jwt.encode(
            {
                "sub": USER_UUID,
                "role": "user",
                "jti": str(uuid.uuid4()),
                "iat": now,
                "exp": now + timedelta(minutes=60),
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
        with pytest.raises(HTTPException) as exc:
            get_current_user(request=_request(), token=untyped, db=None)
        assert exc.value.status_code == 401


class TestOptionalAuthRejectsForeignTypes:
    def test_mfa_token_yields_anonymous(self, mfa_token):
        assert get_optional_current_user(request=_request(mfa_token), db=None) is None

    def test_refresh_token_yields_anonymous(self, refresh_token):
        assert get_optional_current_user(request=_request(refresh_token), db=None) is None


class TestVerifyTokenRejectsForeignTypes:
    """``core.security.verify_token`` is the WebSocket handshake's verifier."""

    def test_mfa_token_rejected(self, mfa_token):
        with pytest.raises(HTTPException) as exc:
            verify_token(mfa_token)
        assert exc.value.status_code == 401

    def test_refresh_token_rejected(self, refresh_token):
        with pytest.raises(HTTPException) as exc:
            verify_token(refresh_token)
        assert exc.value.status_code == 401

    def test_access_token_accepted(self, access_token):
        assert verify_token(access_token)["sub"] == USER_UUID

    def test_opt_out_still_available(self, refresh_token):
        """expected_type=None is the deliberate opt-out for purpose-agnostic reads."""
        assert verify_token(refresh_token, expected_type=None)["sub"] == USER_UUID
