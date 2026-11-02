"""Login and the directory sweep must compare the same username form against ``admin_users``.

Login compares the entry's username-attribute value. The sweep used to compare the stored
``ldap_uid`` cut at its first ``@``, so with a username attribute whose values carry an ``@``
(``userPrincipalName``) an account listed in ``admin_users`` was an admin at login and not an
admin to the sweep, which reconciles on that signal and would demote it.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import ldap_authenticate
from app.auth.ldap_probe import DIRECTORY_PRESENT
from app.auth.ldap_probe import probe_ldap_user
from tests.fixtures.ldap_directory_fake import SEARCH_BASE
from tests.fixtures.ldap_directory_fake import SERVICE_CREDENTIAL
from tests.fixtures.ldap_directory_fake import SERVICE_DN
from tests.fixtures.ldap_directory_fake import FakeDirectory
from tests.fixtures.ldap_directory_fake import FakeEntry
from tests.fixtures.ldap_directory_fake import bound_service_connection
from tests.fixtures.ldap_directory_fake import install

UPN = "jane@corp.example"
JANE_DN = "cn=jane,dc=test"
JANE_PW = "jane-pw"


def _cfg(**overrides: Any) -> LdapConfig:
    base = LdapConfig(
        enabled=True,
        server="ldap.test",
        use_ssl=True,
        bind_dn=SERVICE_DN,
        bind_password=SERVICE_CREDENTIAL,
        search_base=SEARCH_BASE,
        username_attr="userPrincipalName",
        user_search_filter="(userPrincipalName={username})",
    )
    return dataclasses.replace(base, **overrides)


def _directory() -> FakeDirectory:
    d = FakeDirectory()
    entry = FakeEntry(
        JANE_DN, {"userPrincipalName": UPN, "mail": "jane.doe@mail.example", "cn": "Jane"}
    )
    d.add_user(entry, JANE_PW, filter_text="(userPrincipalName=jane)")
    return d


def _login_then_sweep(monkeypatch, cfg: LdapConfig) -> tuple[bool, bool]:
    d = install(monkeypatch, _directory())
    monkeypatch.setattr(LdapConfig, "from_env", classmethod(lambda cls: cfg))

    login = ldap_authenticate("jane", JANE_PW)
    assert login is not None
    assert login["username"] == UPN

    probe = probe_ldap_user(cfg, bound_service_connection(d), login["username"], login["email"])
    assert probe.status == DIRECTORY_PRESENT
    return login["is_admin"], probe.is_admin


@pytest.mark.parametrize("listed", [UPN, "  JANE@Corp.Example ", f"bob, {UPN}"])
def test_an_admin_whose_username_has_an_at_sign_stays_admin_in_the_sweep(monkeypatch, listed):
    assert _login_then_sweep(monkeypatch, _cfg(admin_users=listed)) == (True, True)


def test_the_uid_before_the_at_sign_is_not_an_admin_in_either_path(monkeypatch):
    assert _login_then_sweep(monkeypatch, _cfg(admin_users="jane")) == (False, False)


def test_an_unlisted_user_is_not_an_admin_in_either_path(monkeypatch):
    assert _login_then_sweep(monkeypatch, _cfg(admin_users="bob@corp.example")) == (False, False)
