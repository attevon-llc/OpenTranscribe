"""Saving an auth-config value must change what the system does.

Every other auth-config test asserts *storage*: write a key, read it back, see
the same string. All 30 dead keys passed tests like that. These assert the other
half — that the value reaches the code that enforces it — with one case per
category in ``CATEGORY_SCHEMAS``.

Each test writes through ``AuthConfigService.bulk_update_category``, which is the
exact path ``PUT /api/admin/auth-config/{category}`` takes, so the process-wide
cache is primed the same way it is in production. Reads then go through the
consumer, never back through the config API.
"""

from __future__ import annotations

import pytest

from app.auth import lockout
from app.auth.mfa import MFAService
from app.auth.password_policy import validate_password
from app.services.auth_config_service import AuthConfigService

pytestmark = pytest.mark.xdist_group("auth_config_behaviour")


def save(db, user, category: str, config: dict) -> None:
    """Write a category the way the admin endpoint does."""
    AuthConfigService.bulk_update_category(
        db=db, category=category, config_dict=config, user_id=user.id
    )


@pytest.fixture
def isolated_lockout_store(monkeypatch):
    """Give the lockout plane a private in-memory store for this test."""
    monkeypatch.setattr(lockout, "_redis_client", None)
    monkeypatch.setattr(lockout, "_in_memory_store", lockout.InMemoryLockoutStore())
    monkeypatch.setattr(lockout, "_store_initialized", True)


# ── local ───────────────────────────────────────────────────────────────────────


def test_local_allow_registration_closes_the_endpoint(client, db_session, super_admin_user):
    """Turning self-registration off must make POST /auth/register refuse."""
    save(db_session, super_admin_user, "local", {"allow_registration": False})

    response = client.post(
        "/api/auth/register",
        json={
            "email": "should-not-exist@example.com",
            "password": "Str0ng!Passphrase99",
            "full_name": "Nope",
        },
    )

    assert response.status_code == 403, response.text
    assert "disabled" in response.json()["detail"].lower()


# ── password_policy ─────────────────────────────────────────────────────────────


#: Every complexity switch on, minimum at the floor — written in full by each
#: password test so the assertion never depends on the deployment's .env values.
_BASE_POLICY = {
    "password_policy_enabled": True,
    "password_min_length": 8,
    "password_require_uppercase": True,
    "password_require_lowercase": True,
    "password_require_digit": True,
    "password_require_special": True,
}


def test_password_min_length_is_enforced_at_validation(db_session, super_admin_user):
    """A raised minimum must reject a password that was acceptable before."""
    candidate = "Sh0rt!Pass"  # 10 chars: fine at 8, rejected at 20

    save(db_session, super_admin_user, "password_policy", _BASE_POLICY)
    assert validate_password(candidate).is_valid

    save(db_session, super_admin_user, "password_policy", {"password_min_length": 20})
    result = validate_password(candidate)
    assert not result.is_valid
    assert any("at least 20 characters" in error for error in result.errors)


def test_password_require_special_can_be_relaxed(db_session, super_admin_user):
    """Clearing a complexity requirement must actually stop rejecting."""
    candidate = "NoSymbolsHere123"

    save(db_session, super_admin_user, "password_policy", _BASE_POLICY)
    assert not validate_password(candidate).is_valid

    save(db_session, super_admin_user, "password_policy", {"password_require_special": False})
    assert validate_password(candidate).is_valid


def test_password_policy_can_be_switched_off_entirely(db_session, super_admin_user):
    """password_policy_enabled=False must stop rejecting, not just soften."""
    save(db_session, super_admin_user, "password_policy", _BASE_POLICY)
    assert not validate_password("short").is_valid

    save(db_session, super_admin_user, "password_policy", {"password_policy_enabled": False})
    assert validate_password("short").is_valid


# ── lockout ─────────────────────────────────────────────────────────────────────


def test_lockout_threshold_locks_on_the_configured_attempt(
    db_session, super_admin_user, isolated_lockout_store
):
    """threshold=3 must lock on the third failure, not the .env fifth."""
    save(
        db_session,
        super_admin_user,
        "lockout",
        {"account_lockout_enabled": True, "account_lockout_threshold": 3},
    )
    identifier = "threshold-probe@example.com"

    assert lockout.check_and_record_attempt(identifier, success=False) == (False, None)
    assert lockout.check_and_record_attempt(identifier, success=False) == (False, None)

    is_locked, unlock_at = lockout.check_and_record_attempt(identifier, success=False)
    assert is_locked, "third failure did not lock: the saved threshold was ignored"
    assert unlock_at is not None


def test_lockout_duration_comes_from_the_saved_value(
    db_session, super_admin_user, isolated_lockout_store
):
    """The lockout must last as long as the admin said, not as long as .env said."""
    save(
        db_session,
        super_admin_user,
        "lockout",
        {
            "account_lockout_enabled": True,
            "account_lockout_threshold": 1,
            "account_lockout_duration_minutes": 90,
            "account_lockout_progressive": False,
        },
    )

    is_locked, unlock_at = lockout.check_and_record_attempt("duration@example.com", success=False)
    assert is_locked and unlock_at is not None

    from datetime import UTC
    from datetime import datetime

    minutes = (unlock_at - datetime.now(UTC)).total_seconds() / 60
    assert 88 < minutes <= 90, f"lockout lasted ~{minutes:.0f} min, expected 90"


def test_lockout_can_be_switched_off_entirely(db_session, super_admin_user, isolated_lockout_store):
    """account_lockout_enabled=False must stop counting, not just stop locking."""
    save(db_session, super_admin_user, "lockout", {"account_lockout_enabled": False})

    for _ in range(10):
        assert lockout.check_and_record_attempt("off@example.com", success=False) == (False, None)
    assert lockout.get_lockout_info("off@example.com")["failed_attempts"] == 0


# ── mfa ─────────────────────────────────────────────────────────────────────────


def test_mfa_backup_code_count_is_honoured(db_session, super_admin_user):
    """The number of codes handed out must be the number the admin chose."""
    save(db_session, super_admin_user, "mfa", {"mfa_backup_code_count": 3})
    assert len(MFAService.generate_backup_codes()) == 3

    save(db_session, super_admin_user, "mfa", {"mfa_backup_code_count": 12})
    assert len(MFAService.generate_backup_codes()) == 12


def test_mfa_issuer_name_reaches_the_qr_code(client, db_session, super_admin_user, normal_user):
    """The issuer an admin sets must be the one the authenticator app shows."""
    save(
        db_session,
        super_admin_user,
        "mfa",
        {"mfa_enabled": True, "mfa_issuer_name": "Acme Secure Portal"},
    )

    token = client.post(
        "/api/auth/token",
        data={"username": normal_user.email, "password": "password123"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert token.status_code == 200, token.text

    response = client.post(
        "/api/auth/mfa/setup",
        headers={"Authorization": f"Bearer {token.json()['access_token']}"},
    )
    assert response.status_code == 200, response.text
    assert "Acme%20Secure%20Portal" in response.json()["provisioning_uri"]


def test_mfa_token_lifetime_reaches_the_half_token(db_session, super_admin_user):
    """The half-token's exp must move with the saved value."""
    from jose import jwt

    from app.api.endpoints.auth.mfa_tokens import _create_mfa_token
    from app.core.config import settings

    save(db_session, super_admin_user, "mfa", {"mfa_token_expire_minutes": 17})

    claims = jwt.decode(
        _create_mfa_token("00000000-0000-0000-0000-000000000001", "user"),
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )
    lifetime_minutes = (claims["exp"] - claims["iat"]) / 60
    assert 16.5 < lifetime_minutes < 17.5, f"half-token lived {lifetime_minutes:.1f} min"


# ── banner ──────────────────────────────────────────────────────────────────────


def test_banner_text_is_served_to_the_login_page(client, db_session, super_admin_user):
    """AC-8: the banner endpoint must return what the admin typed."""
    save(
        db_session,
        super_admin_user,
        "banner",
        {
            "login_banner_enabled": True,
            "login_banner_text": "AUTHORIZED USE ONLY",
            "login_banner_classification": "CUI",
        },
    )

    body = client.get("/api/auth/banner").json()
    assert body["enabled"] is True
    assert body["text"] == "AUTHORIZED USE ONLY"
    assert body["classification"] == "CUI"


# ── session ─────────────────────────────────────────────────────────────────────


def test_max_concurrent_sessions_terminates_the_oldest(client, db_session, super_admin_user):
    """AC-10: a limit of 1 must leave exactly one live session after two logins."""
    from app.models.refresh_token import RefreshToken

    save(
        db_session,
        super_admin_user,
        "session",
        {"max_concurrent_sessions": 1, "concurrent_session_policy": "terminate_oldest"},
    )

    credentials = {"username": super_admin_user.email, "password": "superadminpass"}
    for _ in range(2):
        response = client.post(
            "/api/auth/token",
            data=credentials,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200, response.text

    live = (
        db_session.query(RefreshToken)
        .filter(
            RefreshToken.user_id == super_admin_user.id,
            RefreshToken.revoked_at.is_(None),
        )
        .count()
    )
    assert live == 1, f"{live} live sessions with max_concurrent_sessions=1"


# ── ldap ────────────────────────────────────────────────────────────────────────


def test_ldap_server_reaches_the_connection_config(db_session, super_admin_user):
    """The directory the authenticator will dial must be the saved one."""
    from app.auth.ldap_auth import LdapConfig

    save(
        db_session,
        super_admin_user,
        "ldap",
        {"ldap_enabled": True, "ldap_server": "dc01.example.test", "ldap_port": 3269},
    )

    config = LdapConfig.from_db(db_session)
    assert config.enabled is True
    assert config.server == "dc01.example.test"
    assert config.port == 3269


# ── keycloak ────────────────────────────────────────────────────────────────────


def test_keycloak_roles_claim_reaches_the_token_mapper(db_session, super_admin_user):
    """Reading the wrong claim means everyone logs in and nobody is an admin."""
    from app.auth.keycloak_auth import KeycloakConfig

    save(db_session, super_admin_user, "keycloak", {"keycloak_roles_claim": "groups"})

    assert KeycloakConfig.from_db(db_session).roles_claim == "groups"


# ── pki ─────────────────────────────────────────────────────────────────────────


def test_pki_enabled_opens_and_closes_the_authenticate_route(client, db_session, super_admin_user):
    """Disabled must refuse outright; enabled must get as far as needing a cert."""

    def detail(response) -> str:
        try:
            return str(response.json().get("detail", "")).lower()
        except ValueError:
            return str(response.text).lower()

    save(db_session, super_admin_user, "pki", {"pki_enabled": False})
    disabled = client.post("/api/auth/pki/authenticate")
    assert disabled.status_code == 400
    assert "not enabled" in detail(disabled)

    save(db_session, super_admin_user, "pki", {"pki_enabled": True})
    enabled = client.post("/api/auth/pki/authenticate")
    assert "not enabled" not in detail(enabled), (
        "PKI authenticate still reports 'not enabled' after pki_enabled was saved True"
    )
