"""The admin LDAP "Test connection" binds with the password a login would use.

The saved bind password is never returned to the browser, so after a save the panel
submits that field blank. The test used to read blank as "anonymous bind" and report a
refusal for a configuration every real login accepted. It must resolve the password the
way a login does (saved and decrypted, else ``LDAP_BIND_PASSWORD``), let a typed password
win, and never send the saved one to a target other than the saved one.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.endpoints.auth_config_connection_tests import _test_ldap_connection
from app.auth import ldap_directory
from app.core.config import settings
from app.services.auth_config_service import SENSITIVE_SET_SENTINEL
from app.services.auth_config_service import AuthConfigService

pytestmark = pytest.mark.xdist_group("auth_config")

TEST_URL = "/api/admin/auth-config/ldap/test"
SERVER = "ldaps://directory.saved.example"
BIND_DN = "cn=svc,dc=saved,dc=example"
SAVED_SECRET = "saved-bind-secret"
ENV_SECRET = "env-bind-secret"


@pytest.fixture
def binds(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record every bind the connection test attempts, and attempt none for real."""
    seen: list[tuple[str, str]] = []

    def fake_connect_and_bind(cfg: Any, server: Any, user: str, password: str) -> None:
        seen.append((user, password))
        return None

    monkeypatch.setattr(ldap_directory, "_get_ldap_server", lambda cfg: object())
    monkeypatch.setattr(ldap_directory, "transport_problem", lambda cfg, server: None)
    monkeypatch.setattr(ldap_directory, "_connect_and_bind", fake_connect_and_bind)
    # Pin the environment so the stored row, not a stray LDAP_* value, decides.
    monkeypatch.setattr(settings, "LDAP_SERVER", "")
    monkeypatch.setattr(settings, "LDAP_BIND_DN", "")
    monkeypatch.setattr(settings, "LDAP_BIND_PASSWORD", "")
    monkeypatch.setattr(settings, "LDAP_CA_CERT_FILE", "")
    monkeypatch.setattr(settings, "LDAP_TLS_SERVER_NAME", "")
    monkeypatch.setattr(settings, "LDAP_TLS_SKIP_VERIFY", False)
    return seen


@pytest.fixture
def saved_ldap(db_session: Any, super_admin_user: Any) -> None:
    """Save an LDAP configuration with an encrypted bind password, as the panel does."""
    AuthConfigService.bulk_update_category(
        db_session,
        "ldap",
        {
            "ldap_server": SERVER,
            "ldap_port": 636,
            "ldap_use_ssl": True,
            "ldap_use_tls": False,
            "ldap_bind_dn": BIND_DN,
            "ldap_bind_password": SAVED_SECRET,
        },
        user_id=int(super_admin_user.id),
    )
    stored = AuthConfigService.get_config_by_category(db_session, "ldap", decrypt=False)
    assert stored["ldap_bind_password"] == SENSITIVE_SET_SENTINEL


def _form(**overrides: Any) -> dict[str, Any]:
    """The LDAP panel's form after loading the saved config (no secret in it)."""
    form: dict[str, Any] = {
        "ldap_server": SERVER,
        "ldap_port": 636,
        "ldap_use_ssl": True,
        "ldap_use_tls": False,
        "ldap_ca_cert_file": "",
        "ldap_tls_server_name": "",
        "ldap_tls_skip_verify": None,
        "ldap_bind_dn": BIND_DN,
        "ldap_bind_password": "",
    }
    form.update(overrides)
    return form


def test_blank_password_after_save_binds_with_the_saved_secret(
    client: Any, super_admin_token_headers: Any, saved_ldap: None, binds: list[tuple[str, str]]
) -> None:
    resp = client.post(TEST_URL, headers=super_admin_token_headers, json=_form())

    assert resp.status_code == 200
    assert binds == [(BIND_DN, SAVED_SECRET)]
    assert SAVED_SECRET not in resp.text


@pytest.mark.parametrize(
    "submitted",
    [SENSITIVE_SET_SENTINEL, "***REDACTED***", "***ENCRYPTED***", None, "absent"],
)
def test_a_placeholder_or_missing_password_also_takes_the_saved_secret(
    db_session: Any, saved_ldap: None, binds: list[tuple[str, str]], submitted: Any
) -> None:
    form = _form(ldap_bind_password=submitted)
    if submitted == "absent":
        del form["ldap_bind_password"]

    _test_ldap_connection(form, db_session)

    assert binds == [(BIND_DN, SAVED_SECRET)]


def test_a_typed_password_wins_over_the_saved_one(
    client: Any, super_admin_token_headers: Any, saved_ldap: None, binds: list[tuple[str, str]]
) -> None:
    resp = client.post(
        TEST_URL, headers=super_admin_token_headers, json=_form(ldap_bind_password="typed-new")
    )

    assert resp.status_code == 200
    assert binds == [(BIND_DN, "typed-new")]


@pytest.fixture
def env_ldap(binds: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure LDAP through the ``LDAP_*`` environment only, with nothing saved."""
    monkeypatch.setattr(settings, "LDAP_SERVER", SERVER)
    monkeypatch.setattr(settings, "LDAP_PORT", 636)
    monkeypatch.setattr(settings, "LDAP_USE_SSL", True)
    monkeypatch.setattr(settings, "LDAP_USE_TLS", False)
    monkeypatch.setattr(settings, "LDAP_BIND_DN", BIND_DN)
    monkeypatch.setattr(settings, "LDAP_BIND_PASSWORD", ENV_SECRET)


def test_nothing_saved_falls_back_to_the_environment_password(
    db_session: Any, env_ldap: None, binds: list[tuple[str, str]]
) -> None:
    assert AuthConfigService.get_config_by_category(db_session, "ldap") == {}

    _test_ldap_connection(_form(), db_session)

    assert binds == [(BIND_DN, ENV_SECRET)]


@pytest.mark.parametrize(
    "changed",
    [
        {"ldap_server": "ldaps://elsewhere.example"},
        {"ldap_port": 3269},
        {"ldap_bind_dn": "cn=other,dc=saved,dc=example"},
        {"ldap_tls_skip_verify": True},
        {"ldap_use_ssl": False, "ldap_use_tls": True},
    ],
    ids=["server", "port", "bind-dn", "skip-verify", "transport"],
)
def test_the_saved_secret_never_goes_to_a_different_target(
    client: Any,
    super_admin_token_headers: Any,
    saved_ldap: None,
    binds: list[tuple[str, str]],
    changed: dict[str, Any],
) -> None:
    resp = client.post(TEST_URL, headers=super_admin_token_headers, json=_form(**changed))

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["message"].startswith("Enter the bind password to test")
    assert binds == []
    assert SAVED_SECRET not in resp.text


def test_a_regular_admin_cannot_use_the_saved_secret(
    client: Any, admin_token_headers: Any, saved_ldap: None, binds: list[tuple[str, str]]
) -> None:
    resp = client.post(TEST_URL, headers=admin_token_headers, json=_form())

    assert resp.status_code == 403
    assert binds == []
