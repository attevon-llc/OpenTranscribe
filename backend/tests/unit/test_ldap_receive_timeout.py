"""Every LDAP connection carries a read timeout, not only a connect timeout.

``Server(connect_timeout=...)`` bounds the TCP connect. Without ``receive_timeout`` on the
``Connection``, a directory that accepts the connection and then never answers holds the
calling thread (a login request, or the directory sweep) for as long as the socket stays
open. Both are bounded by the one ``ldap_timeout`` setting, and a timed-out bind is refused.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from ldap3.core.exceptions import LDAPSocketReceiveError

from app.auth import ldap_auth
from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import _connect_and_bind


def _cfg(**overrides: Any) -> LdapConfig:
    return dataclasses.replace(LdapConfig(server="ldap.test", use_ssl=True, timeout=7), **overrides)


class _Conn:
    options: list[dict[str, Any]] = []
    bind_error: BaseException | None = None

    def __init__(self, server: Any, user: str, password: str, auto_bind: bool, **options: Any):
        type(self).options.append(options)
        self.bound = False

    def bind(self) -> bool:
        error = type(self).bind_error
        if error is not None:
            raise error
        self.bound = True
        return True

    def unbind(self) -> bool:
        return True


def test_connect_and_bind_passes_the_configured_read_timeout(monkeypatch):
    _Conn.options, _Conn.bind_error = [], None
    monkeypatch.setattr(ldap_auth, "Connection", _Conn)

    assert _connect_and_bind(_cfg(), object(), "cn=a", "pw") is not None

    assert _Conn.options == [{"receive_timeout": 7}]


def test_a_directory_that_never_answers_is_refused_with_a_clear_log(monkeypatch, caplog):
    _Conn.options, _Conn.bind_error = [], LDAPSocketReceiveError("timed out")
    monkeypatch.setattr(ldap_auth, "Connection", _Conn)

    with caplog.at_level("ERROR", logger="app.auth.ldap_auth"):
        assert _connect_and_bind(_cfg(), object(), "cn=a", "pw") is None

    assert "did not answer within 7 s" in caplog.text
