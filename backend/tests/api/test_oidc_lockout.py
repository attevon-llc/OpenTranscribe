"""``GET /api/auth/oidc/callback`` records failed/successful attempts in the lockout
mechanism, the same way ``/token`` does for a local-password login.

Every other stage of the OIDC callback (state single-use, PKCE, ID-token validation,
admission) is covered elsewhere (``test_oidc_state_single_use.py``, ``test_oidc_admission.py``,
``test_oidc_auth.py``). This suite monkeypatches those stages to a fixed outcome so it can
isolate the property under test: that ``check_and_record_attempt`` is actually called on the
identity-bearing failure/success points of this handler, which it previously never was at all.
"""

from __future__ import annotations

import uuid as uuid_pkg
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.endpoints.auth import oidc as oidc_module
from app.auth.lockout import check_and_record_attempt
from app.auth.lockout import get_lockout_info
from app.auth.lockout import unlock_account
from app.core.security import get_password_hash
from app.db.base import get_db
from app.main import app
from app.models.user import User
from app.services.auth_config_service import AuthConfigService

pytestmark = pytest.mark.xdist_group("unknown_identifier_lockout")

ENDPOINT = "/api/auth/oidc/callback"
STATE = "fixed-state-value"
BINDING_SECRET = "fixed-binding-secret"


@pytest.fixture
def oidc_enabled(db_session, super_admin_user):
    AuthConfigService.bulk_update_category(
        db_session,
        "oidc",
        {"oidc_enabled": True},
        user_id=int(super_admin_user.id),
    )
    yield


@pytest.fixture
def oidc_client(db_session, monkeypatch):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    # State + PKCE + binding-cookie verification are covered elsewhere; fix them
    # to "already valid" so this suite can isolate the lockout-recording wiring.
    monkeypatch.setattr(
        oidc_module._oidc_state_store,
        "get_state",
        lambda state: {"binding": oidc_module._hash_binding(BINDING_SECRET)},
    )
    monkeypatch.setattr(oidc_module, "get_oidc_state_binding", lambda request: BINDING_SECRET)

    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _oidc_user(db_session, **overrides) -> User:
    user = User(
        email=f"oidc-{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name="OIDC Person",
        hashed_password=get_password_hash("irrelevant-Passphrase99!"),
        role="user",
        auth_type="oidc",
        oidc_subject=f"subj-{uuid_pkg.uuid4().hex}",
        is_active=True,
        is_superuser=False,
    )
    for key, value in overrides.items():
        setattr(user, key, value)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


_FAKE_TOKENS = SimpleNamespace(access_token="at", id_token="idt", refresh_token=None)


async def _fake_exchange(code, code_verifier, cfg):
    return _FAKE_TOKENS


class TestFailedExchangeRecordsAnAttempt:
    def test_token_exchange_failure_increments_the_unknown_bucket(
        self, oidc_client, monkeypatch, oidc_enabled
    ):
        async def _failing_exchange(code, code_verifier, cfg):
            return None

        monkeypatch.setattr(oidc_module, "exchange_code_for_tokens", _failing_exchange)

        # "unknown" is the shared pre-identity-failure bucket (oidc.py and saml.py both
        # write it deliberately -- see the comment at oidc.py:249) and ACCOUNT_LOCKOUT_
        # THRESHOLD defaults to 5 with no relaxed override in CI (docker-compose.override.
        # yml is never loaded there). Without a reset, enough tests across both files
        # accumulate failed attempts on "unknown" to lock it -- and a locked identifier's
        # check_and_record_attempt returns early WITHOUT incrementing failed_attempts, so
        # a later test's `after == before + 1` silently fails as `after == before`. Reset
        # via the real "successful login clears the counter" path before every snapshot,
        # so this test's baseline is deterministic regardless of what ran before it.
        unlock_account("unknown")
        check_and_record_attempt("unknown", success=True)
        before = get_lockout_info("unknown")["failed_attempts"]
        response = oidc_client.get(ENDPOINT, params={"code": "irrelevant", "state": STATE})
        after = get_lockout_info("unknown")["failed_attempts"]

        assert response.status_code == 401
        assert after == before + 1


class TestInvalidTokenRecordsAnAttempt:
    def test_invalid_id_token_increments_the_unknown_bucket(
        self, oidc_client, monkeypatch, oidc_enabled
    ):
        monkeypatch.setattr(oidc_module, "exchange_code_for_tokens", _fake_exchange)

        async def _failing_validate(access_token, cfg, id_token):
            return None

        monkeypatch.setattr(oidc_module, "validate_oidc_token", _failing_validate)

        # See the reset comment in TestFailedExchangeRecordsAnAttempt above -- same
        # shared-bucket-vs-lockout-threshold reasoning applies here.
        unlock_account("unknown")
        check_and_record_attempt("unknown", success=True)
        before = get_lockout_info("unknown")["failed_attempts"]
        response = oidc_client.get(ENDPOINT, params={"code": "irrelevant", "state": STATE})
        after = get_lockout_info("unknown")["failed_attempts"]

        assert response.status_code == 401
        assert after == before + 1


class TestSuccessfulLoginClearsTheCounter:
    def test_success_resets_the_identifiers_own_counter(
        self, oidc_client, db_session, monkeypatch, oidc_enabled
    ):
        user = _oidc_user(db_session)
        monkeypatch.setattr(oidc_module, "exchange_code_for_tokens", _fake_exchange)

        async def _fake_validate(access_token, cfg, id_token):
            return {
                "oidc_subject": user.oidc_subject,
                "email": user.email,
                "email_verified": True,
                "full_name": user.full_name,
                "groups": [],
                "claim_keys": [],
                "roles_claim_source": "none",
            }

        monkeypatch.setattr(oidc_module, "validate_oidc_token", _fake_validate)
        monkeypatch.setattr(oidc_module, "sync_oidc_user_to_db", lambda db, data, cfg: user)

        check_and_record_attempt(user.email, success=False)
        assert get_lockout_info(user.email)["failed_attempts"] >= 1

        with patch.object(oidc_module.settings, "JWT_ACCESS_TOKEN_EXPIRE_MINUTES", 30):
            response = oidc_client.get(ENDPOINT, params={"code": "irrelevant", "state": STATE})

        assert response.status_code == 200, response.text
        assert get_lockout_info(user.email)["failed_attempts"] == 0
