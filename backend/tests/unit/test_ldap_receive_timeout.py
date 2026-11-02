"""Every LDAP connection carries a read timeout, not only a connect timeout.

``Server(connect_timeout=...)`` bounds the TCP connect. Without ``receive_timeout`` on the
``Connection``, a directory that accepts the connection and then never answers holds the
calling thread (a login request, or the directory sweep) for as long as the socket stays
open. Both are bounded by the one ``ldap_timeout`` setting.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import ldap_authenticate
from app.auth.ldap_probe import ldap_directory_session
from tests.fixtures.ldap_directory_fake import SEARCH_BASE
from tests.fixtures.ldap_directory_fake import SERVICE_CREDENTIAL
from tests.fixtures.ldap_directory_fake import SERVICE_DN
from tests.fixtures.ldap_directory_fake import FakeEntry
from tests.fixtures.ldap_directory_fake import install

ALICE_PW = "alice-pw"


def _cfg(**overrides: Any) -> LdapConfig:
    base = LdapConfig(
        enabled=True,
        server="ldap.test",
        use_ssl=True,
        bind_dn=SERVICE_DN,
        bind_password=SERVICE_CREDENTIAL,
        search_base=SEARCH_BASE,
        user_search_filter="(uid={username})",
        timeout=7,
    )
    return dataclasses.replace(base, **overrides)


def test_login_opens_both_connections_with_the_configured_read_timeout(monkeypatch):
    d = install(monkeypatch)
    d.add_user(FakeEntry("uid=alice,dc=test", {"uid": "alice", "mail": "a@test.example"}), ALICE_PW)
    monkeypatch.setattr(LdapConfig, "from_env", classmethod(lambda cls: _cfg()))

    assert ldap_authenticate("alice", ALICE_PW) is not None

    assert [c[1] for c in d.calls if c[0] == "connection"] == [SERVICE_DN, "uid=alice,dc=test"]
    assert d.connection_options == [{"receive_timeout": 7}, {"receive_timeout": 7}]
    assert [c[2]["connect_timeout"] for c in d.calls if c[0] == "server"] == [7]


def test_the_sweep_session_uses_the_same_read_timeout(monkeypatch):
    d = install(monkeypatch)

    with ldap_directory_session(_cfg(timeout=3)):
        pass

    assert d.connection_options == [{"receive_timeout": 3}]
