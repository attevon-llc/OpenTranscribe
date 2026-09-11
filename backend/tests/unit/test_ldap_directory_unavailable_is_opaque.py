"""#914 STEP 5: LdapDirectoryUnavailableError must never echo the raw ldap3
exception -- not from ``str()``, not from any of its consumers.

An ``ldap3`` ``LDAPException`` raised while binding the service account or
searching the directory can quote the directory server's own URL and the
service-account bind DN. ``auth/ldap_auth.py``'s four raise sites used to
interpolate that text (``f"LDAP bind failed: {type(e).__name__}: {e}"``)
straight into ``LdapDirectoryUnavailableError``'s message, which then reached:
an admin's 503 body (``POST /admin/group-mappings/test``), and a directory-sync
report dict (``sweep_ldap()["error"]``) persisted to ``SystemSettings`` and
rendered on ``GET /admin/directory-sync/status``. Fixed by scrubbing once at
the raiser, per the repo's "one owner" preference — this file proves both
consumers inherit the fix for free.
"""

from __future__ import annotations

import logging

import pytest
from ldap3.core.exceptions import LDAPBindError

from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import LdapDirectoryUnavailableError
from app.auth.ldap_auth import ldap_directory_session

#: The exact shape #914 named: a directory URL and a service-account bind DN.
DIRECTORY_URL_SENTINEL = "ldaps://dc01.corp.internal:636"
BIND_DN_SENTINEL = "cn=svc-bind,ou=svc,dc=corp"


def _sentinel_config() -> LdapConfig:
    return LdapConfig(
        enabled=True,
        server="dc01.corp.internal",
        port=636,
        use_ssl=True,
        bind_dn=BIND_DN_SENTINEL,
        bind_password="irrelevant",  # noqa: S106 - test fixture, not a real secret
        search_base="dc=corp",
    )


def _raise_bind_failure(*_args, **_kwargs):
    raise LDAPBindError(
        f"Can't contact LDAP server: {DIRECTORY_URL_SENTINEL} (bind DN {BIND_DN_SENTINEL} rejected)"
    )


def test_ldap_directory_session_never_echoes_the_raw_ldap3_message(monkeypatch, caplog):
    """Direct call: LdapDirectoryUnavailableError's own str() is opaque."""
    monkeypatch.setattr("app.auth.ldap_auth._bind_service_account", _raise_bind_failure)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(LdapDirectoryUnavailableError) as excinfo:
            with ldap_directory_session(_sentinel_config()):
                pass  # pragma: no cover - never reached

    message = str(excinfo.value)
    assert DIRECTORY_URL_SENTINEL not in message
    assert BIND_DN_SENTINEL not in message
    assert "LDAPBindError" in message

    # The real cause is still diagnosable via the log.
    assert DIRECTORY_URL_SENTINEL in caplog.text
    assert BIND_DN_SENTINEL in caplog.text


def test_group_mapping_test_endpoint_never_leaks_the_directory_url(
    super_admin_token_headers, client, monkeypatch, caplog
):
    """POST /admin/group-mappings/test -- admin_group_mappings.py::_ldap_claims_for's
    503 body. The allowlisted raise-path finding there is `{exc}` interpolated
    from the ALREADY-sanitized LdapDirectoryUnavailableError, so this proves the
    allowlist reason is still honest end to end."""
    from app.auth import ldap_auth

    monkeypatch.setattr(ldap_auth, "_bind_service_account", _raise_bind_failure)
    monkeypatch.setattr(
        ldap_auth.LdapConfig, "from_db", classmethod(lambda cls, db: _sentinel_config())
    )

    with caplog.at_level(logging.ERROR):
        response = client.post(
            "/api/admin/group-mappings/test",
            headers=super_admin_token_headers,
            json={"source": "ldap", "username": "someuser"},
        )

    assert response.status_code == 503
    assert DIRECTORY_URL_SENTINEL not in response.text
    assert BIND_DN_SENTINEL not in response.text
    assert DIRECTORY_URL_SENTINEL in caplog.text
    assert BIND_DN_SENTINEL in caplog.text


def test_sweep_ldap_report_never_leaks_the_directory_url(db_session, monkeypatch, caplog):
    """directory_sync_service.sweep_ldap()["error"] -- persisted to
    SystemSettings and rendered on GET /admin/directory-sync/status.

    ``probe_users`` is a generator that resolves ``LdapConfig.from_db`` and
    enters ``ldap_directory_session`` BEFORE its first ``yield``, unconditionally
    -- so with zero candidate users (a fresh test DB has none) the directory
    bind still runs the instant ``sweep_ldap``'s ``for`` loop calls ``next()``
    on it. No candidate-user fixture needed to reach the failure path.
    """
    from app.services import directory_sync_service as dss

    monkeypatch.setattr("app.auth.ldap_auth._bind_service_account", _raise_bind_failure)
    monkeypatch.setattr(dss.LdapConfig, "from_db", classmethod(lambda cls, db: _sentinel_config()))

    cfg = dss.SweepConfig(dry_run=True, max_disables=5)

    with caplog.at_level(logging.ERROR):
        result = dss.sweep_ldap(db_session, cfg)

    assert result["status"] == "directory_unavailable"
    assert result["error"] is not None
    assert DIRECTORY_URL_SENTINEL not in result["error"]
    assert BIND_DN_SENTINEL not in result["error"]
    assert DIRECTORY_URL_SENTINEL in caplog.text
    assert BIND_DN_SENTINEL in caplog.text

    dss.record_result(db_session, result)
    db_session.commit()

    persisted = dss.get_settings(db_session)
    persisted_text = str(persisted.get("last_result"))
    assert DIRECTORY_URL_SENTINEL not in persisted_text
    assert BIND_DN_SENTINEL not in persisted_text
