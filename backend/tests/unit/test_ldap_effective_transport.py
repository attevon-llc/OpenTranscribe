"""Transport security is decided from the EFFECTIVE ldap3 server, not from ``cfg.use_ssl``.

ldap3 lets the URL scheme override ``use_ssl``: ``Server('ldap://h', use_ssl=True)`` is a
plaintext server on 389. These tests build the REAL ``Server`` (construction opens no
socket) through ``_get_ldap_server`` and fake only ``Connection``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api.endpoints.auth_config import _test_ldap_connection
from app.auth import ldap_auth
from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import _connect_and_bind
from app.auth.ldap_auth import _get_ldap_server


class _Conn:
    instances: list[_Conn] = []

    def __init__(self, server: Any, user: str = "", password: str = "", **kwargs: Any) -> None:
        self.server = server
        self.calls: list[str] = []
        self.bound = False
        _Conn.instances.append(self)

    def open(self) -> bool:
        self.calls.append("open")
        return True

    def start_tls(self) -> bool:
        self.calls.append("start_tls")
        return True

    def bind(self) -> bool:
        self.calls.append("bind")
        self.bound = True
        return True

    def unbind(self) -> bool:
        self.calls.append("unbind")
        return True


@pytest.fixture(autouse=True)
def fake_connection(monkeypatch: pytest.MonkeyPatch) -> type[_Conn]:
    _Conn.instances = []
    monkeypatch.setattr(ldap_auth, "Connection", _Conn)
    return _Conn


def _cfg(server: str, *, use_ssl: bool, use_tls: bool) -> LdapConfig:
    return LdapConfig(enabled=True, server=server, use_ssl=use_ssl, use_tls=use_tls)


def _bind(cfg: LdapConfig) -> Any:
    return _connect_and_bind(cfg, _get_ldap_server(cfg), "cn=svc", "pw")


def test_url_scheme_really_overrides_use_ssl() -> None:
    assert _get_ldap_server(_cfg("ldap://dir.example", use_ssl=True, use_tls=False)).ssl is False


def test_ldap_url_with_default_use_ssl_and_no_starttls_is_refused() -> None:
    assert _bind(_cfg("ldap://dir.example", use_ssl=True, use_tls=False)) is None
    assert not any("bind" in c.calls for c in _Conn.instances)


@pytest.mark.parametrize("use_ssl", [True, False])
def test_ldap_url_with_starttls_upgrades_before_bind(use_ssl: bool) -> None:
    conn = _bind(_cfg("ldap://dir.example", use_ssl=use_ssl, use_tls=True))
    assert conn is not None
    assert conn.calls == ["open", "start_tls", "bind"]


def test_ldaps_url_binds_over_tls_without_starttls() -> None:
    conn = _bind(_cfg("ldaps://dir.example", use_ssl=True, use_tls=False))
    assert conn is not None
    assert conn.calls == ["bind"]


def test_connection_test_names_the_ldap_url_ssl_contradiction() -> None:
    result = _test_ldap_connection(
        {
            "ldap_server": "ldap://dir.example",
            "ldap_port": 389,
            "ldap_use_ssl": True,
            "ldap_use_tls": False,
            "ldap_bind_dn": "cn=svc",
            "ldap_bind_password": "svc-secret-value",
        }
    )
    assert result.success is False
    assert "ldap_use_ssl is enabled" in result.message
    assert "ldaps://" in result.message
    assert "svc-secret-value" not in result.message
    assert not any("bind" in c.calls for c in _Conn.instances)
