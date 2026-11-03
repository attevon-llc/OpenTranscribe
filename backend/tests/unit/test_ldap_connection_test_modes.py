"""The admin LDAP "Test connection": the plaintext mode, a refused bind, and its timeout.

``test_ldap_connection_test_tls.py`` pins the StartTLS and LDAPS sequences. Left open there:
with neither SSL nor StartTLS the test makes one plain bind (no ``open`` / ``start_tls``), a
refused bind reports the generic failure, the server's ``info`` read for the details comes
from the server object the bind used, and the probe honours the submitted ``ldap_timeout``.

``test_the_submitted_timeout_bounds_the_test`` is written for the CORRECT behaviour and fails
on the code it was written against (P3 defect D-18): the test hard-codes a 10 s timeout, so a
directory that needs longer tests red but logs in fine, and the reverse with a short one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import ldap3
import pytest

from app.api.endpoints.auth_config_connection_tests import _test_ldap_connection
from app.auth import ldap_directory

GENERIC_FAILURE = "LDAP connection failed. Please verify server address, port, and credentials."


class _Server:
    instances: list[_Server] = []

    def __init__(self, host: str, **kwargs: Any) -> None:
        self.host = host
        self.kwargs = kwargs
        self.info = SimpleNamespace(
            vendor_name="Test Directory",
            vendor_version="1.0",
            naming_contexts=["dc=test"],
        )
        _Server.instances.append(self)


class _Connection:
    instances: list[_Connection] = []
    bind_result = True

    def __init__(self, server: Any, user: str = "", password: str = "", **kwargs: Any) -> None:
        self.server = server
        self.user = user
        self.password = password
        self.kwargs = kwargs
        self.calls: list[str] = []
        self.bound = False
        if kwargs.get("auto_bind"):
            self.calls.append("bind")
            self.bound = True
        _Connection.instances.append(self)

    def open(self) -> bool:
        self.calls.append("open")
        return True

    def start_tls(self) -> bool:
        self.calls.append("start_tls")
        return True

    def bind(self) -> bool:
        self.calls.append("bind")
        self.bound = type(self).bind_result
        return self.bound

    def unbind(self) -> bool:
        self.calls.append("unbind")
        self.bound = False
        return True


@pytest.fixture
def fake_ldap(monkeypatch: pytest.MonkeyPatch) -> type[_Connection]:
    _Server.instances = []
    _Connection.instances = []
    _Connection.bind_result = True
    for target in (ldap3, ldap_directory):
        monkeypatch.setattr(target, "Server", _Server)
        monkeypatch.setattr(target, "Connection", _Connection)
    return _Connection


def _config(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ldap_server": "ldap.example",
        "ldap_port": 389,
        "ldap_use_ssl": False,
        "ldap_use_tls": False,
        "ldap_bind_dn": "cn=svc,dc=example",
        "ldap_bind_password": "svc-password",
    }
    base.update(overrides)
    return base


def test_plaintext_mode_makes_one_plain_bind(fake_ldap: type[_Connection]) -> None:
    result = _test_ldap_connection(_config())

    assert result.success is True
    (conn,) = fake_ldap.instances
    assert conn.calls == ["bind", "unbind"]
    assert (conn.user, conn.password) == ("cn=svc,dc=example", "svc-password")
    assert _Server.instances[0].kwargs["use_ssl"] is False


def test_a_refused_bind_reports_the_generic_failure(fake_ldap: type[_Connection]) -> None:
    fake_ldap.bind_result = False

    result = _test_ldap_connection(_config())

    assert result.success is False
    assert result.message == GENERIC_FAILURE
    (conn,) = fake_ldap.instances
    assert conn.calls == ["bind"]


def test_success_reports_the_server_info_of_the_server_it_bound_to(
    fake_ldap: type[_Connection],
) -> None:
    result = _test_ldap_connection(_config())

    assert result.success is True
    assert result.details == {
        "server_info": {
            "vendor_name": "Test Directory",
            "vendor_version": "1.0",
            "naming_contexts": ["dc=test"],
        }
    }
    (server,) = _Server.instances
    assert fake_ldap.instances[0].server is server


@pytest.mark.parametrize("missing", ["ldap_bind_dn", "ldap_bind_password"])
def test_a_half_configured_service_account_binds_anonymously(
    fake_ldap: type[_Connection], missing: str
) -> None:
    result = _test_ldap_connection(_config(**{missing: ""}))

    assert result.success is True
    (conn,) = fake_ldap.instances
    assert (conn.user, conn.password) == ("", "")


def test_a_blank_server_is_refused_before_any_connection(fake_ldap: type[_Connection]) -> None:
    result = _test_ldap_connection(_config(ldap_server=""))

    assert result.success is False
    assert result.message == "LDAP server address is required"
    assert fake_ldap.instances == [] and _Server.instances == []


def test_without_a_submitted_timeout_the_default_ten_seconds_applies(
    fake_ldap: type[_Connection],
) -> None:
    assert _test_ldap_connection(_config()).success is True

    assert _Server.instances[0].kwargs["connect_timeout"] == 10
    assert fake_ldap.instances[0].kwargs["receive_timeout"] == 10


def test_the_submitted_timeout_bounds_the_test(fake_ldap: type[_Connection]) -> None:
    """P3 defect D-18: the test must time out where a login with this config would."""
    assert _test_ldap_connection(_config(ldap_timeout=25)).success is True

    assert _Server.instances[0].kwargs["connect_timeout"] == 25
    assert fake_ldap.instances[0].kwargs["receive_timeout"] == 25
