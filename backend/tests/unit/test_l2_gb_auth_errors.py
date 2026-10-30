"""Characterization of the auth-layer refusals that L2 moves from HTTPException to domain errors.

Every refusal here sits inside authentication or admission, so its wire shape is a
security contract: the uniform 401 for login/admission refusals (an account-existence
oracle otherwise), the ``WWW-Authenticate`` header, and the 403 for an unverified
local login. These tests assert status, exact detail and headers on whichever of
``HTTPException`` / the matching domain error the function raises, so they are green
before and after the conversion; the endpoint-path tests additionally pin how the
callers *react* (lockout recording, local fallback, SAML logout, WebSocket 4003).
"""

# mypy: disable-error-code="arg-type"
# Structural stand-ins (dict payloads, fake users) are passed to signatures that
# declare the real dataclasses/TypedDicts; declared once here instead of a cast at
# every call site.
from __future__ import annotations

import time
import uuid as uuid_pkg
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from joserfc import jwt
from joserfc.jwk import OctKey
from starlette.requests import Request

from app.api.endpoints.auth import authenticators
from app.api.endpoints.auth import login as login_module
from app.api.endpoints.auth import saml as saml_module
from app.auth import account_linking
from app.auth import email_verification
from app.auth import external_sync
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.auth.cookies import ACCESS_COOKIE
from app.auth.oidc.admission import assert_oidc_admission_permitted
from app.auth.oidc.config import OIDCConfig
from app.auth.provider_registry import ExternalIdentity
from app.auth.proxy.assertion import REFUSAL_DETAIL as PROXY_REFUSAL_DETAIL
from app.auth.proxy.assertion import ProxyAssertion
from app.auth.proxy.config import ProxyConfig
from app.auth.proxy.provisioning import sync_proxy_user_to_db
from app.auth.saml.admission import assert_saml_admission_permitted
from app.auth.saml.config import SAMLConfig
from app.core import security
from app.core.auth_types import TOKEN_TYPE_ACCESS
from app.core.auth_types import TOKEN_TYPE_REFRESH
from app.core.config import settings
from app.core.exceptions import AccessDeniedError
from app.core.exceptions import AuthenticationError
from app.core.exceptions import DomainError
from app.core.exceptions import ExternalIdentityLinkRefusedError
from app.models.user import User

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("l2_gb_auth_errors")]

BEARER = {"WWW-Authenticate": "Bearer"}
CERTIFICATE = {"WWW-Authenticate": "Certificate"}


def _refusal(info: pytest.ExceptionInfo[Exception]) -> HTTPException | DomainError:
    """Narrow a tuple-typed ``pytest.raises`` result (mypy joins the tuple to ``Exception``)."""
    value = info.value
    assert isinstance(value, (HTTPException, DomainError))
    return value


class _FakeUser:
    def __init__(self, email="victim@example.com", role="user", auth_type="ldap"):
        self.id = 1
        self.email = email
        self.role = role
        self.auth_type = auth_type
        self.platform_super_admin_link_authorized = False


# -- account_linking (#1, #2) --------------------------------------------------


@pytest.mark.parametrize("headers", [None, BEARER, CERTIFICATE])
def test_email_link_refusal_is_a_401_with_the_callers_detail_and_headers(headers):
    with pytest.raises((HTTPException, AuthenticationError)) as exc:
        account_linking.assert_email_link_permitted(
            _FakeUser(),
            provider="ldap",
            source_identifier="uid",
            email_verified=False,
            failure_detail="Incorrect username or password",
            failure_headers=headers,
        )
    assert _refusal(exc).status_code == 401
    assert _refusal(exc).detail == "Incorrect username or password"
    assert _refusal(exc).headers == headers


def test_email_link_refusal_of_a_super_admin_even_with_a_verified_email():
    with pytest.raises((HTTPException, AuthenticationError)) as exc:
        account_linking.assert_email_link_permitted(
            _FakeUser(role="super_admin"),
            provider="oidc",
            source_identifier="sub",
            email_verified=True,
            failure_detail="Invalid access token",
        )
    assert (_refusal(exc).status_code, _refusal(exc).detail, _refusal(exc).headers) == (
        401,
        "Invalid access token",
        None,
    )


@pytest.mark.parametrize("headers", [None, BEARER, CERTIFICATE])
def test_provider_id_link_refusal_is_a_401_with_the_callers_detail_and_headers(headers):
    with pytest.raises((HTTPException, AuthenticationError)) as exc:
        account_linking.assert_provider_id_link_permitted(
            _FakeUser(email="victim@example.com"),
            provider="ldap",
            source_identifier="recycled",
            asserted_email="attacker@example.com",
            failure_detail="Incorrect username or password",
            failure_headers=headers,
        )
    assert _refusal(exc).status_code == 401
    assert _refusal(exc).detail == "Incorrect username or password"
    assert _refusal(exc).headers == headers


def test_external_sync_rewraps_a_link_refusal_as_its_own_error():
    class _Session:
        def query(self, _model):
            return SimpleNamespace(
                filter=lambda *_a, **_k: SimpleNamespace(
                    first=lambda: _FakeUser(email="victim@example.com", auth_type="cloud")
                )
            )

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    identity = ExternalIdentity(
        provider="cloud",
        external_id="ext-1",
        email="attacker@example.com",
        email_verified=True,
        full_name="A",
    )
    with pytest.raises(ExternalIdentityLinkRefusedError) as exc:
        external_sync.sync_external_user_to_db(_Session(), identity)
    assert exc.value.message == "External identity could not be verified"


# -- email verification (#3) ---------------------------------------------------


def _local_user(db_session, **overrides) -> User:
    params: dict[str, Any] = {
        "email": f"unverified_{uuid_pkg.uuid4().hex[:8]}@example.com",
        "full_name": "U",
        "hashed_password": "x",
        "is_active": True,
        "role": "user",
        "auth_type": "local",
        "email_verified": False,
    }
    params.update(overrides)
    user = User(**params)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_unverified_local_login_is_a_403_and_resends_the_link(db_session, monkeypatch):
    user = _local_user(db_session)
    sent: list[str] = []
    monkeypatch.setattr(email_verification, "email_verification_required", lambda _db: True)
    monkeypatch.setattr(
        email_verification, "issue_verification_token", lambda _db, u, *a: sent.append(u.email)
    )

    with pytest.raises((HTTPException, AccessDeniedError)) as exc:
        email_verification.assert_email_verified_for_local_login(db_session, str(user.uuid))

    assert _refusal(exc).status_code == 403
    assert _refusal(exc).detail == (
        "Your email address has not been verified. Check your inbox for the link."
    )
    assert _refusal(exc).headers is None
    assert sent == [user.email]


def test_unverified_local_login_over_the_real_endpoint_answers_403(client, db_session, monkeypatch):
    user = _local_user(db_session, hashed_password=_hash("password123"))
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setattr(email_verification, "email_verification_required", lambda _db: True)
    monkeypatch.setattr(email_verification, "issue_verification_token", lambda *_a, **_k: None)

    response = client.post(
        "/api/auth/token",
        data={"username": user.email, "password": "password123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Your email address has not been verified. Check your inbox for the link."
    }


def _hash(password: str) -> str:
    from app.core.security import get_password_hash

    return get_password_hash(password)


# -- OIDC / SAML / proxy admission (#4-#7) -------------------------------------


def test_oidc_blocked_group_is_a_401_with_the_uniform_detail():
    cfg = OIDCConfig(enabled=True, blocked_groups="Contractors")
    with pytest.raises((HTTPException, AuthenticationError)) as exc:
        assert_oidc_admission_permitted(
            {"oidc_subject": "s", "email": "a@example.com", "roles": ["Contractors"]},
            cfg,
            failure_detail="Invalid access token",
        )
    assert (_refusal(exc).status_code, _refusal(exc).detail, _refusal(exc).headers) == (
        401,
        "Invalid access token",
        None,
    )


def test_oidc_unknown_groups_with_a_configured_list_is_the_same_401():
    cfg = OIDCConfig(enabled=True, allowed_groups="Staff")
    with pytest.raises((HTTPException, AuthenticationError)) as exc:
        assert_oidc_admission_permitted(
            {"oidc_subject": "s", "email": "a@example.com", "roles": [], "groups_overage": True},
            cfg,
            failure_detail="Invalid access token",
        )
    assert (_refusal(exc).status_code, _refusal(exc).detail, _refusal(exc).headers) == (
        401,
        "Invalid access token",
        None,
    )


def test_saml_not_allowed_group_is_a_401_with_the_uniform_detail():
    cfg = SAMLConfig(enabled=True, sp_entity_id="https://sp.example.com", allowed_groups="Staff")
    with pytest.raises((HTTPException, AuthenticationError)) as exc:
        assert_saml_admission_permitted(
            {
                "saml_subject": "s",
                "email": "a@example.com",
                "email_verified": False,
                "full_name": "A",
                "groups": ["Everyone"],
                "is_admin": False,
            },
            cfg,
            failure_detail="Invalid SAML assertion",
        )
    assert (_refusal(exc).status_code, _refusal(exc).detail, _refusal(exc).headers) == (
        401,
        "Invalid SAML assertion",
        None,
    )


def test_proxy_refusal_for_an_unknown_account_is_the_generic_401(db_session):
    assertion = ProxyAssertion(
        email=f"nobody_{uuid_pkg.uuid4().hex[:8]}@example.com", full_name="N"
    )
    cfg = ProxyConfig(enabled=True, jit_provisioning=False)

    with pytest.raises((HTTPException, AuthenticationError)) as exc:
        sync_proxy_user_to_db(db_session, assertion, cfg)

    assert (_refusal(exc).status_code, _refusal(exc).detail, _refusal(exc).headers) == (
        401,
        PROXY_REFUSAL_DETAIL,
        None,
    )
    assert db_session.query(User).filter(User.email == assertion.email).first() is None
    db_session.rollback()


def test_proxy_super_admin_link_refusal_is_the_same_generic_401(db_session):
    admin = _local_user(db_session, role="super_admin", is_superuser=True)
    assertion = ProxyAssertion(email=str(admin.email), full_name="A")

    with pytest.raises((HTTPException, AuthenticationError)) as exc:
        sync_proxy_user_to_db(db_session, assertion, ProxyConfig(enabled=True))

    assert (_refusal(exc).status_code, _refusal(exc).detail, _refusal(exc).headers) == (
        401,
        PROXY_REFUSAL_DETAIL,
        None,
    )
    db_session.rollback()


# -- LDAP login refusal over the real login path (R1) --------------------------


def _ldap_login_setup(db_session, monkeypatch):
    uid = f"ldapuid_{uuid_pkg.uuid4().hex[:8]}"
    victim = _local_user(
        db_session,
        auth_type="ldap",
        ldap_uid=uid,
        email=f"victim_{uuid_pkg.uuid4().hex[:8]}@example.com",
        hashed_password="!",
    )
    monkeypatch.setenv("TESTING", "false")
    real = authenticators.get_auth_settings
    monkeypatch.setattr(
        authenticators,
        "get_auth_settings",
        lambda db: SimpleNamespace(ldap_enabled=True, _real=real(db)),
    )
    monkeypatch.setattr(
        authenticators,
        "ldap_authenticate",
        lambda username, password, db=None: {
            "username": uid,
            "email": "attacker@example.com",
            "full_name": "Attacker",
            "is_admin": False,
            "groups": [],
        },
    )
    return uid, victim


def test_ldap_link_refusal_is_a_plain_failed_attempt_to_perform_authentication(
    db_session, monkeypatch
):
    uid, _victim = _ldap_login_setup(db_session, monkeypatch)

    result = login_module._perform_authentication(db_session, uid, "whatever")

    assert result == (False, "", {}, "")


def test_ldap_link_refusal_over_the_endpoint_is_the_uniform_401_and_records_the_attempt(
    client, db_session, monkeypatch
):
    uid, _victim = _ldap_login_setup(db_session, monkeypatch)
    attempts: list[tuple[str, bool]] = []

    def _record(identifier, success, exempt_from_lockout=False):
        attempts.append((identifier, success))
        return False, None

    monkeypatch.setattr(login_module, "check_and_record_attempt", _record)

    response = client.post(
        "/api/auth/token",
        data={"username": uid, "password": "whatever"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Incorrect username or password"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert attempts, "the refused login must still be recorded for lockout"
    assert all(success is False for _identifier, success in attempts)


def test_ldap_email_match_without_a_local_row_is_the_uniform_401_and_one_failed_attempt(
    client, db_session, monkeypatch
):
    """The takeover case: the directory user has no local row but asserts a victim's email.

    With no local account, ``authenticators`` has no local fallback to turn the refusal
    into a fresh 401, so the link refusal itself must reach ``login._perform_authentication``
    and be treated as a failed attempt rather than escaping unrecorded.
    """
    victim = _local_user(db_session, hashed_password="!")
    ghost = f"ghost_{uuid_pkg.uuid4().hex[:8]}"
    monkeypatch.setenv("TESTING", "false")
    # _authenticate_ldap_user imports get_auth_settings inside the function, so the
    # module-level name in ``authenticators`` is not the one it calls. Patching only
    # that name leaves LDAP "disabled" and the test would take the plain HTTPException
    # 401 instead of the link-refusal DomainError this case exists to pin.
    from app.core import auth_settings as auth_settings_module

    real = auth_settings_module.get_auth_settings

    class _LdapOn:
        def __init__(self, inner):
            self._inner = inner
            self.ldap_enabled = True

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(auth_settings_module, "get_auth_settings", lambda db: _LdapOn(real(db)))
    monkeypatch.setattr(
        authenticators,
        "ldap_authenticate",
        lambda username, password, db=None: {
            "username": ghost,
            "email": victim.email,
            "full_name": "Attacker",
            "is_admin": False,
            "groups": [],
        },
    )
    attempts: list[tuple[str, bool]] = []

    def _record(identifier, success, exempt_from_lockout=False):
        attempts.append((identifier, success))
        return False, None

    monkeypatch.setattr(login_module, "check_and_record_attempt", _record)

    response = client.post(
        "/api/auth/token",
        data={"username": ghost, "password": "whatever"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Incorrect username or password"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert len(attempts) == 1
    assert attempts[0][1] is False


# -- verify_token (#9, #10) ----------------------------------------------------


def _mint(claims: dict[str, Any]) -> str:
    key = OctKey.import_key(settings.JWT_SECRET_KEY)
    return jwt.encode({"alg": settings.JWT_ALGORITHM}, claims, key)


def test_verify_token_rejects_a_garbage_token_with_bearer_challenge():
    with pytest.raises((HTTPException, AuthenticationError)) as exc:
        security.verify_token("not-a-jwt")
    assert _refusal(exc).status_code == 401
    assert _refusal(exc).detail == "Invalid authentication credentials"
    assert _refusal(exc).headers == BEARER


def test_verify_token_rejects_a_wrong_token_type_with_bearer_challenge():
    token = _mint({"sub": "1", "type": TOKEN_TYPE_REFRESH, "exp": int(time.time()) + 600})
    with pytest.raises((HTTPException, AuthenticationError)) as exc:
        security.verify_token(token)
    assert _refusal(exc).status_code == 401
    assert _refusal(exc).detail == "Invalid authentication credentials"
    assert _refusal(exc).headers == BEARER


def test_verify_token_failure_text_is_what_persisted_error_text_has_always_been():
    # str(exc) is stored and notified by some callers; the status prefix is part of it.
    with pytest.raises((HTTPException, AuthenticationError)) as exc:
        security.verify_token("not-a-jwt")
    assert str(_refusal(exc)) == "401: Invalid authentication credentials"


def test_verify_token_still_accepts_a_valid_token():
    token = _mint({"sub": "1", "type": TOKEN_TYPE_ACCESS, "exp": int(time.time()) + 600})
    assert security.verify_token(token)["sub"] == "1"


# -- callers of verify_token: SAML logout swallow (R4), WebSocket 4003 (R5) ----


class _StubAuth:
    def process_slo(self, delete_session_cb=None, **_k):
        if delete_session_cb is not None:
            delete_session_cb()
        return "https://idp.example.com/after-logout"

    def get_errors(self) -> list[str]:
        return []


def test_saml_logout_with_an_unverifiable_access_cookie_is_still_a_clean_success(
    db_session, monkeypatch
):
    records: list[dict] = []
    original = audit_logger.log

    def _capture(*args, **kwargs):
        records.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(audit_logger, "log", _capture)
    monkeypatch.setattr(saml_module, "_build_auth", lambda *_a, **_k: _StubAuth())
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "headers": [(b"cookie", f"{ACCESS_COOKIE}=garbage".encode())],
        }
    )

    response = saml_module._complete_saml_sls(
        request, db_session, SAMLConfig(enabled=True), {}, "203.0.113.9", "pytest"
    )

    assert response.status_code == 302
    assert response.headers["location"] == "https://idp.example.com/after-logout"
    (record,) = [r for r in records if r.get("event_type") == AuditEventType.AUTH_LOGOUT]
    assert record["outcome"] == AuditOutcome.SUCCESS
    assert record["details"]["processing_failed"] is False
    assert record["details"]["revocation_failed"] is False


def test_websocket_token_authentication_returns_none_for_an_invalid_token(db_session):
    from app.api.websockets import _try_authenticate_token

    assert _try_authenticate_token("not-a-jwt", db_session) is None
