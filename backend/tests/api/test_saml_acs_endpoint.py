"""``POST /api/auth/saml/acs`` — the MFA and lockout wiring, not the SAML protocol.

Signature verification and the assertion parse are python3-saml's / ``saml/assertion.py``'s
and are exercised elsewhere (``test_saml_sp.py``, ``test_saml_admission.py``). This suite
monkeypatches ``build_auth`` / ``extract_saml_user_data`` / ``sync_saml_user_to_db`` to a
verified-and-parsed assertion for a real, already-provisioned ``User`` row, so it can pin
two properties of the handler itself with a real DB and a real ``TestClient``:

* an MFA-enrolled SAML user must be challenged, not handed a full session, exactly as
  ``/token`` challenges a local-password login (``_check_mfa_requirement`` was previously
  never called on this path at all);
* a failed SAML authentication increments the same lockout counter a failed local-password
  attempt does (``check_and_record_attempt`` was previously never called on this path either).
"""

from __future__ import annotations

import uuid as uuid_pkg
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from app.api.endpoints.auth import saml as saml_module
from app.auth.lockout import get_lockout_info
from app.core.config import settings
from app.core.security import get_password_hash
from app.db.base import get_db
from app.main import app
from app.models.user import User
from app.models.user_mfa import UserMFA
from app.services.auth_config_service import AuthConfigService

pytestmark = pytest.mark.xdist_group("unknown_identifier_lockout")

ENDPOINT = "/api/auth/saml/acs"


class _FakeSamlAuth:
    """Stands in for ``OneLogin_Saml2_Auth`` post ``process_response()``."""

    def process_response(self) -> None:
        return None

    def get_errors(self) -> list:
        return []

    def is_authenticated(self) -> bool:
        return True

    def get_last_error_reason(self) -> str:
        return ""


@pytest.fixture
def saml_enabled(db_session, super_admin_user):
    AuthConfigService.bulk_update_category(
        db_session,
        "saml",
        {"saml_enabled": True},
        user_id=int(super_admin_user.id),
    )
    yield


@pytest.fixture
def saml_client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def _saml_user(db_session, **overrides) -> User:
    user = User(
        email=f"saml-{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name="SAML Person",
        hashed_password=get_password_hash("irrelevant-Passphrase99!"),
        role="user",
        auth_type="saml",
        is_active=True,
        is_superuser=False,
    )
    for key, value in overrides.items():
        setattr(user, key, value)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _mock_saml_round_trip(monkeypatch, user: User) -> None:
    """Patch the handler's three python3-saml-facing calls to a verified
    assertion for ``user``, leaving the handler's own post-authentication
    logic (MFA gate, lockout recording, cookie issuance) genuinely exercised."""
    monkeypatch.setattr(saml_module, "build_auth", lambda request_data, cfg: _FakeSamlAuth())
    monkeypatch.setattr(
        saml_module,
        "extract_saml_user_data",
        lambda auth, cfg: {
            "saml_subject": f"subj-{user.email}",
            "email": user.email,
            "email_verified": False,
            "full_name": user.full_name,
            "groups": [],
            "is_admin": False,
        },
    )
    monkeypatch.setattr(saml_module, "sync_saml_user_to_db", lambda db, saml_data, cfg: user)


class TestSamlLoginRespectsEnrolledMfa:
    def test_enrolled_user_is_redirected_to_the_mfa_challenge_not_sessioned(
        self, saml_client, db_session, monkeypatch, saml_enabled
    ):
        user = _saml_user(db_session)
        db_session.add(UserMFA(user_id=user.id, totp_secret="irrelevant", totp_enabled=True))
        db_session.commit()
        _mock_saml_round_trip(monkeypatch, user)

        with patch.object(settings, "MFA_ENABLED", True):
            response = saml_client.post(ENDPOINT, data={"SAMLResponse": "irrelevant-base64"})

        assert response.status_code == 302
        assert "set-cookie" not in {k.lower() for k in response.headers}
        location = response.headers["location"]
        assert urlparse(location).path == "/login"
        query = parse_qs(urlparse(location).query)
        assert query["mfa_required"] == ["true"]
        assert query["mfa_token"][0]

        from app.models.refresh_token import RefreshToken

        sessions = db_session.query(RefreshToken).filter(RefreshToken.user_id == user.id).count()
        assert sessions == 0, "no session may be minted before the second factor is verified"

    def test_unenrolled_user_still_gets_a_full_session(
        self, saml_client, db_session, monkeypatch, saml_enabled
    ):
        user = _saml_user(db_session)
        _mock_saml_round_trip(monkeypatch, user)

        with patch.object(settings, "MFA_ENABLED", True):
            response = saml_client.post(ENDPOINT, data={"SAMLResponse": "irrelevant-base64"})

        assert response.status_code == 302
        cookie_names = {c.split("=")[0] for c in response.headers.get_list("set-cookie")}
        assert "access_token" in cookie_names


class TestSamlLoginRecordsLockoutAttempts:
    def test_a_rejected_assertion_records_a_failed_attempt(
        self, saml_client, db_session, monkeypatch, saml_enabled
    ):
        class _RejectingAuth(_FakeSamlAuth):
            def get_errors(self) -> list:
                return ["invalid_response"]

            def is_authenticated(self) -> bool:
                return False

        monkeypatch.setattr(saml_module, "build_auth", lambda request_data, cfg: _RejectingAuth())

        before = get_lockout_info("unknown")["failed_attempts"]
        response = saml_client.post(ENDPOINT, data={"SAMLResponse": "garbage"})
        after = get_lockout_info("unknown")["failed_attempts"]

        assert response.status_code == 401
        assert after == before + 1

    def test_a_successful_login_clears_the_counter(
        self, saml_client, db_session, monkeypatch, saml_enabled
    ):
        user = _saml_user(db_session)
        _mock_saml_round_trip(monkeypatch, user)

        # Accrue a failure against this exact identifier first.
        from app.auth.lockout import check_and_record_attempt

        check_and_record_attempt(user.email, success=False)
        assert get_lockout_info(user.email)["failed_attempts"] >= 1

        response = saml_client.post(ENDPOINT, data={"SAMLResponse": "irrelevant-base64"})

        assert response.status_code == 302
        assert get_lockout_info(user.email)["failed_attempts"] == 0
