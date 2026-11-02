"""The admin LDAP "Test connection" binds exactly the way login does.

``_test_ldap_connection`` built its own ``ldap3.Connection(auto_bind=True)`` and never
read ``ldap_use_tls``, so on a StartTLS deployment it sent the service-account
password over the plaintext connection, while login (``ldap_auth._connect_and_bind``)
negotiates StartTLS and refuses the bind when that fails. It could also report success
for a configuration login then refused.
"""

from __future__ import annotations

from typing import Any

import ldap3
import pytest

from app.api.endpoints.auth_config import _test_ldap_connection
from app.auth import ldap_auth

GENERIC_FAILURE = "LDAP connection failed. Please verify server address, port, and credentials."


class _FakeServer:
    def __init__(self, host: str, **kwargs: Any) -> None:
        self.host = host
        self.kwargs = kwargs
        self.info = None


class _FakeConnection:
    instances: list[_FakeConnection] = []
    start_tls_result = True

    def __init__(self, server: Any, user: str = "", password: str = "", **kwargs: Any) -> None:
        self.server = server
        self.user = user
        self.password = password
        self.calls: list[str] = []
        self.bound = False
        # ldap3 binds inside the constructor when auto_bind is set.
        if kwargs.get("auto_bind"):
            self.calls.append("bind")
            self.bound = True
        _FakeConnection.instances.append(self)

    def open(self) -> bool:
        self.calls.append("open")
        return True

    def start_tls(self) -> bool:
        self.calls.append("start_tls")
        return type(self).start_tls_result

    def bind(self) -> bool:
        self.calls.append("bind")
        self.bound = True
        return True

    def unbind(self) -> bool:
        self.calls.append("unbind")
        self.bound = False
        return True


@pytest.fixture
def fake_ldap(monkeypatch: pytest.MonkeyPatch) -> type[_FakeConnection]:
    _FakeConnection.instances = []
    _FakeConnection.start_tls_result = True
    # Both the library and the login path's bindings, so whichever one the test
    # route uses never reaches the network.
    for target in (ldap3, ldap_auth):
        monkeypatch.setattr(target, "Server", _FakeServer)
        monkeypatch.setattr(target, "Connection", _FakeConnection)
    return _FakeConnection


def _config(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ldap_server": "ldap.example",
        "ldap_port": 389,
        "ldap_use_ssl": False,
        "ldap_use_tls": True,
        "ldap_bind_dn": "cn=svc,dc=example",
        "ldap_bind_password": "svc-password",
    }
    base.update(overrides)
    return base


def test_failed_starttls_refuses_the_bind(fake_ldap: type[_FakeConnection]) -> None:
    fake_ldap.start_tls_result = False

    result = _test_ldap_connection(_config())

    assert result.success is False
    assert result.message == GENERIC_FAILURE
    assert [c.calls for c in fake_ldap.instances] == [["open", "start_tls"]]


def test_starttls_is_negotiated_before_the_bind(fake_ldap: type[_FakeConnection]) -> None:
    result = _test_ldap_connection(_config())

    assert result.success is True
    (conn,) = fake_ldap.instances
    assert conn.calls == ["open", "start_tls", "bind", "unbind"]
    assert (conn.user, conn.password) == ("cn=svc,dc=example", "svc-password")


def test_ldaps_binds_directly_without_starttls(fake_ldap: type[_FakeConnection]) -> None:
    result = _test_ldap_connection(_config(ldap_port=636, ldap_use_ssl=True))

    assert result.success is True
    (conn,) = fake_ldap.instances
    assert conn.calls == ["bind", "unbind"]
    assert conn.server.kwargs["use_ssl"] is True


def test_anonymous_bind_still_negotiates_starttls(fake_ldap: type[_FakeConnection]) -> None:
    result = _test_ldap_connection(_config(ldap_bind_dn="", ldap_bind_password=""))

    assert result.success is True
    (conn,) = fake_ldap.instances
    assert conn.calls == ["open", "start_tls", "bind", "unbind"]
    assert (conn.user, conn.password) == ("", "")
