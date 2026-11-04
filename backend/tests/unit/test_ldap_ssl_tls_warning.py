"""The SSL-plus-StartTLS startup warning names the mode the bind code actually uses.

With both ``LDAP_USE_SSL`` and ``LDAP_USE_TLS`` on, ``_connect_and_bind`` binds over the
LDAPS socket and never negotiates StartTLS. The warning used to say the opposite.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import cast

from app.core.config import Settings
from app.core.config import _validate_ldap_settings


def _settings(*, use_ssl: bool, use_tls: bool, server: str = "ldap.test") -> Settings:
    # Only the fields the validator reads; constructing Settings would read the process env.
    stand_in = SimpleNamespace(
        LDAP_ENABLED=True,
        LDAP_SERVER=server,
        LDAP_BIND_DN="cn=svc,dc=test",
        LDAP_BIND_PASSWORD="svc-credential",
        LDAP_SEARCH_BASE="dc=test",
        LDAP_USE_SSL=use_ssl,
        LDAP_USE_TLS=use_tls,
    )
    return cast(Settings, stand_in)


def _warnings(caplog, settings: Settings) -> list[str]:
    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        _validate_ldap_settings(settings)
    return [r.getMessage() for r in caplog.records if r.name == "app.core.config"]


def test_both_on_warns_that_ldaps_is_used_and_starttls_is_not(caplog):
    assert _warnings(caplog, _settings(use_ssl=True, use_tls=True)) == [
        "LDAP_USE_SSL and LDAP_USE_TLS are mutually exclusive. Using LDAPS (LDAP_USE_SSL); "
        "StartTLS is not negotiated."
    ]


def test_both_on_with_an_ldap_url_warns_that_starttls_is_used(caplog):
    # The URL scheme overrides use_ssl in ldap3, so an ldap:// address is never LDAPS and the
    # bind code negotiates StartTLS; the warning must say that, not "StartTLS is not negotiated".
    stand_in = _settings(use_ssl=True, use_tls=True, server="ldap://dir.test")
    assert _warnings(caplog, stand_in) == [
        "LDAP_USE_SSL and LDAP_USE_TLS are mutually exclusive, and LDAP_SERVER is an "
        "ldap:// URL, which is never LDAPS. Using StartTLS (LDAP_USE_TLS); "
        "LDAP_USE_SSL is ignored."
    ]


def test_one_mode_alone_does_not_warn(caplog):
    assert _warnings(caplog, _settings(use_ssl=True, use_tls=False)) == []
    assert _warnings(caplog, _settings(use_ssl=False, use_tls=True)) == []
