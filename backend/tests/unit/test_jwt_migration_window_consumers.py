"""Tokens signed under a PREVIOUS ``JWT_ALGORITHM`` reach every consumer, not just the
request path.

``core.security.accepted_algorithms`` keeps a migration window open so that changing
``JWT_ALGORITHM`` does not sign everybody out. That window only means something if
every decode site honours it. Three did not — they decoded with
``[settings.JWT_ALGORITHM]``:

* **logout** (``sessions._verified_claims``): a token from before the change was
  accepted by every request it authenticated, but at logout it failed to decode,
  so nothing was revoked and the token stayed live until it expired. That is a
  logout that reports success and leaves the session usable.
* **the MFA half-token verifier** (``mfa_tokens._verify_mfa_token``): a user
  half-way through an MFA login at the moment of the change was refused.

``test_jwt_algorithm_single_owner.py`` is the static guard that every decode site
calls the owner; these are the behavioural proofs that doing so changes the outcome.
Tokens are encoded through ``tests/jwt_compat.py``, an independent path into
joserfc, so the "old" token does not depend on any app issuer.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from fastapi import HTTPException

from app.core.auth_types import TOKEN_TYPE_ACCESS
from app.core.auth_types import TOKEN_TYPE_MFA
from app.core.auth_types import TOKEN_TYPE_REFRESH
from app.core.config import settings
from tests.jwt_compat import jwt

#: 64 bytes, so HS512 is a legitimate configuration rather than a warned-about one.
SECRET = "migration-window-suite-hs512-secret-padded-to-64-bytes-012345678"

SUBJECT = "019ec90a-1b2c-7def-8000-00000000a1e7"

#: What the deployment signed with before the operator changed ``JWT_ALGORITHM``.
OLD_ALGORITHM = "HS256"
NEW_ALGORITHM = "HS512"


@pytest.fixture
def algorithm_changed(monkeypatch) -> None:
    """An operator followed the documented route to HS512; the window is open."""
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", SECRET)
    monkeypatch.setattr(settings, "FIPS_MODE", True)
    monkeypatch.setattr(settings, "FIPS_VERSION", "140-3")
    monkeypatch.setattr(settings, "FIPS_MIGRATION_MODE", "compatible")
    monkeypatch.setattr(settings, "JWT_ALGORITHM", NEW_ALGORITHM)
    monkeypatch.setattr(settings, "JWT_ALGORITHM_V3", "HS512")


def _old_token(token_type: str, jti: str, **extra) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": SUBJECT,
        "role": "user",
        "type": token_type,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        **extra,
    }
    return jwt.encode(claims, SECRET, algorithm=OLD_ALGORITHM)


@pytest.fixture
def revocations(monkeypatch) -> list[str]:
    """The jtis logout asked to revoke. The store itself is not under test here."""
    from app.api.endpoints.auth import sessions
    from app.auth.token_service import token_service

    revoked: list[str] = []
    monkeypatch.setattr(
        token_service, "revoke_token", lambda _db, jti, _expires_at=None: revoked.append(jti)
    )
    monkeypatch.setattr(sessions, "publish_token_revocation", lambda *_a, **_k: None)
    return revoked


@pytest.mark.unit
class TestLogoutRevokesTokensFromBeforeTheChange:
    @pytest.mark.parametrize("token_type", [TOKEN_TYPE_ACCESS, TOKEN_TYPE_REFRESH])
    def test_a_bearer_token_signed_with_the_old_algorithm_is_revoked(
        self, client, algorithm_changed, revocations, token_type
    ):
        jti = f"pre-change-{token_type}-jti"
        token = _old_token(token_type, jti)

        resp = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200
        assert revocations == [jti], (
            f"logout did not revoke a {token_type} token signed {OLD_ALGORITHM} after "
            f"JWT_ALGORITHM moved to {NEW_ALGORITHM} — the token the user just 'logged "
            "out' of is still live"
        )

    def test_the_control_a_token_under_a_foreign_key_is_not_revoked(
        self, client, algorithm_changed, revocations
    ):
        """Widening must not mean 'decode anything': a forged token revokes nothing."""
        forged = jwt.encode(
            {
                "sub": SUBJECT,
                "type": TOKEN_TYPE_ACCESS,
                "jti": "forged-jti",
                "exp": int((datetime.now(UTC) + timedelta(minutes=10)).timestamp()),
            },
            "not-the-deployment-secret-" + "x" * 40,
            algorithm=OLD_ALGORITHM,
        )

        resp = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {forged}"})

        assert resp.status_code == 200
        assert revocations == []


@pytest.mark.unit
class TestMfaHalfTokenSurvivesTheChange:
    @pytest.fixture(autouse=True)
    def _blacklist_empty(self, monkeypatch) -> None:
        from app.api.endpoints.auth import mfa_tokens

        monkeypatch.setattr(mfa_tokens, "_is_mfa_token_blacklisted", lambda _jti: False)

    def test_a_half_token_minted_before_the_change_still_verifies(self, algorithm_changed):
        from app.api.endpoints.auth.mfa_tokens import MFA_SCOPE_VERIFY
        from app.api.endpoints.auth.mfa_tokens import _verify_mfa_token

        token = _old_token(TOKEN_TYPE_MFA, "pre-change-mfa-jti", mfa_scope=MFA_SCOPE_VERIFY)

        assert _verify_mfa_token(token) == (SUBJECT, "user", "pre-change-mfa-jti")

    def test_the_control_a_closed_window_refuses_it(self, algorithm_changed, monkeypatch):
        """Strict mode accepts only what is signed now, so the same token is refused."""
        from app.api.endpoints.auth.mfa_tokens import MFA_SCOPE_VERIFY
        from app.api.endpoints.auth.mfa_tokens import _verify_mfa_token

        monkeypatch.setattr(settings, "FIPS_MIGRATION_MODE", "strict")
        token = _old_token(TOKEN_TYPE_MFA, "pre-change-mfa-jti", mfa_scope=MFA_SCOPE_VERIFY)

        with pytest.raises(HTTPException) as exc:
            _verify_mfa_token(token)

        assert exc.value.status_code == 401

    def test_a_new_half_token_is_signed_with_the_current_algorithm(self, algorithm_changed):
        from app.api.endpoints.auth.mfa_tokens import _create_mfa_token

        token = _create_mfa_token(SUBJECT, "user")

        assert jwt.get_unverified_header(token)["alg"] == NEW_ALGORITHM
