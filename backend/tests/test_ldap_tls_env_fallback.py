"""Saving the LDAP panel must not freeze the TLS keys the admin never set.

The admin panel posts its whole form. On an install whose LDAP settings live in the database
but whose CA bundle, TLS server name or skip-verify switch come from ``LDAP_*`` environment
settings, an untouched TLS field arrives as ``""`` / ``None``. Storing that would override the
environment value on the next save of any unrelated LDAP field, and every directory login would
start failing certificate verification. A save with no value for one of those keys removes its
row instead, so the environment keeps applying.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.auth.ldap_auth import LdapConfig
from app.core.config import settings
from app.models.auth_config import AuthConfig
from app.services.auth_config_service import AuthConfigService

pytestmark = pytest.mark.xdist_group("auth_config")

ENV_CA = "/etc/ssl/directory-ca-from-env.pem"
ENV_SERVER_NAME = "directory.env.example"
TLS_KEYS = ("ldap_ca_cert_file", "ldap_tls_server_name", "ldap_tls_skip_verify")


@pytest.fixture
def env_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LDAP_CA_CERT_FILE", ENV_CA)
    monkeypatch.setattr(settings, "LDAP_TLS_SERVER_NAME", ENV_SERVER_NAME)
    monkeypatch.setattr(settings, "LDAP_TLS_SKIP_VERIFY", True)


def _stored(db_session: Session, key: str) -> AuthConfig | None:
    return db_session.query(AuthConfig).filter(AuthConfig.config_key == key).first()


def _save(db_session, admin_user, payload: dict) -> None:
    AuthConfigService.bulk_update_category(
        db=db_session, category="ldap", config_dict=payload, user_id=admin_user.id
    )


def test_untouched_tls_fields_leave_the_environment_in_force(db_session, admin_user, env_tls):
    """The panel's payload for an admin who only changed the server address."""
    _save(
        db_session,
        admin_user,
        {
            "ldap_server": "ldaps://dir.example",
            "ldap_ca_cert_file": "",
            "ldap_tls_server_name": "",
            "ldap_tls_skip_verify": None,
        },
    )

    for key in TLS_KEYS:
        assert _stored(db_session, key) is None, key
    resolved = LdapConfig.from_db(db_session)
    assert resolved.server == "ldaps://dir.example"
    assert resolved.ca_cert_file == ENV_CA
    assert resolved.tls_server_name == ENV_SERVER_NAME
    assert resolved.tls_skip_verify is True


def test_clearing_a_stored_tls_value_hands_it_back_to_the_environment(
    db_session, admin_user, env_tls
):
    _save(
        db_session,
        admin_user,
        {
            "ldap_ca_cert_file": "/etc/ssl/panel-ca.pem",
            "ldap_tls_server_name": "directory.panel.example",
            "ldap_tls_skip_verify": False,
        },
    )
    assert LdapConfig.from_db(db_session).ca_cert_file == "/etc/ssl/panel-ca.pem"

    _save(
        db_session,
        admin_user,
        {"ldap_ca_cert_file": "  ", "ldap_tls_server_name": None, "ldap_tls_skip_verify": None},
    )

    for key in TLS_KEYS:
        assert _stored(db_session, key) is None, key
    resolved = LdapConfig.from_db(db_session)
    assert resolved.ca_cert_file == ENV_CA
    assert resolved.tls_server_name == ENV_SERVER_NAME
    assert resolved.tls_skip_verify is True


def test_an_explicit_choice_still_overrides_the_environment(db_session, admin_user, env_tls):
    """Turning verification back ON in the panel must win over a skip-verify environment."""
    _save(
        db_session,
        admin_user,
        {
            "ldap_ca_cert_file": "/etc/ssl/panel-ca.pem",
            "ldap_tls_server_name": "directory.panel.example",
            "ldap_tls_skip_verify": False,
        },
    )

    resolved = LdapConfig.from_db(db_session)
    assert resolved.ca_cert_file == "/etc/ssl/panel-ca.pem"
    assert resolved.tls_server_name == "directory.panel.example"
    assert resolved.tls_skip_verify is False
    row = _stored(db_session, "ldap_tls_skip_verify")
    assert row is not None
    assert row.config_value == "false"
