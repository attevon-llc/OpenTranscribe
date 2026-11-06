"""A refused LDAP bind must not leave its socket open.

In ldap3 ``Connection.unbind`` is what closes the socket, and a connection that was opened
but never bound still holds one. ``_connect_and_bind`` used to unbind only a *bound*
connection, so after a failed StartTLS and after every refused bind the socket stayed open
until garbage collection. Every refusal path must now unbind, and a failing unbind must be
logged, not raised.
"""

from __future__ import annotations

import logging
from typing import Any
from typing import cast

import pytest
from ldap3 import Connection
from ldap3.core.exceptions import LDAPBindError
from ldap3.core.exceptions import LDAPOperationsErrorResult
from ldap3.core.exceptions import LDAPSocketReceiveError

from app.auth import ldap_directory
from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_directory import _close_connection
from app.auth.ldap_directory import _connect_and_bind
from app.auth.ldap_directory import _get_ldap_server
from tests.fixtures.ldap_directory_fake import SERVICE_CREDENTIAL
from tests.fixtures.ldap_directory_fake import SERVICE_DN
from tests.fixtures.ldap_directory_fake import FakeDirectory
from tests.fixtures.ldap_directory_fake import install


def _ldaps() -> LdapConfig:
    return LdapConfig(enabled=True, server="ldap.test", use_ssl=True, use_tls=False)


def _starttls() -> LdapConfig:
    return LdapConfig(enabled=True, server="ldap.test", use_ssl=False, use_tls=True)


def _wrong_password(d: FakeDirectory) -> None:
    d.passwords[SERVICE_DN] = "not-" + SERVICE_CREDENTIAL


def _bind_rejected(d: FakeDirectory) -> None:
    d.bind_errors[SERVICE_DN] = LDAPBindError("invalidCredentials")


def _bind_times_out(d: FakeDirectory) -> None:
    d.bind_errors[SERVICE_DN] = LDAPSocketReceiveError("timed out")


def _starttls_refused(d: FakeDirectory) -> None:
    d.start_tls_result = False


@pytest.mark.parametrize(
    "cfg,break_directory",
    [
        (_ldaps, _wrong_password),
        (_ldaps, _bind_rejected),
        (_ldaps, _bind_times_out),
        (_starttls, _wrong_password),
        (_starttls, _starttls_refused),
    ],
    ids=[
        "ldaps-wrong-password",
        "ldaps-rejected",
        "ldaps-timeout",
        "starttls-wrong-password",
        "starttls-refused",
    ],
)
def test_a_refused_bind_leaves_the_connection_closed(monkeypatch, cfg: Any, break_directory: Any):
    directory = install(monkeypatch)
    break_directory(directory)
    config = cfg()

    result = _connect_and_bind(config, _get_ldap_server(config), SERVICE_DN, SERVICE_CREDENTIAL)

    assert result is None
    (conn,) = directory.connections
    assert conn.closed is True
    assert directory.unbinds() == [SERVICE_DN]


def test_an_unexpected_ldap_error_still_closes_the_connection_and_propagates(monkeypatch):
    directory = install(monkeypatch)
    directory.bind_errors[SERVICE_DN] = LDAPOperationsErrorResult()
    config = _ldaps()

    with pytest.raises(LDAPOperationsErrorResult):
        _connect_and_bind(config, _get_ldap_server(config), SERVICE_DN, SERVICE_CREDENTIAL)

    (conn,) = directory.connections
    assert conn.closed is True


def test_a_successful_bind_is_returned_open(monkeypatch):
    directory = install(monkeypatch)
    config = _ldaps()

    conn = _connect_and_bind(config, _get_ldap_server(config), SERVICE_DN, SERVICE_CREDENTIAL)

    assert conn is not None and conn.bound is True and conn.closed is False
    assert directory.unbinds() == []


def test_a_failing_unbind_is_logged_not_raised(caplog):
    class _Broken:
        def unbind(self) -> None:
            raise OSError("socket already gone")

    with caplog.at_level(logging.DEBUG, logger=ldap_directory.logger.name):
        _close_connection(cast(Connection, _Broken()), "user")

    assert "Ignored an error while closing the user LDAP connection" in caplog.text
    assert "socket already gone" in caplog.text
