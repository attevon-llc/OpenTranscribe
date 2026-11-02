"""Login and the directory sweep must compare the same username form against ``admin_users``.

Login compares the entry's username-attribute value. The sweep used to compare the stored
``ldap_uid`` cut at its first ``@``, so with a username attribute whose values carry an ``@``
(``userPrincipalName``) an account listed in ``admin_users`` was an admin at login and not an
admin to the sweep, which reconciles on that signal and would demote it.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from app.auth import ldap_auth
from app.auth.ldap_auth import DIRECTORY_PRESENT
from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import _extract_user_attributes
from app.auth.ldap_auth import _is_ldap_admin
from app.auth.ldap_auth import probe_ldap_user

UPN = "jane@corp.example"


class _Entry(SimpleNamespace):
    """Quacks like an ldap3 entry for the attributes the code under test reads."""

    entry_dn = "cn=jane,dc=test"

    def __contains__(self, key: str) -> bool:
        return key in self.__dict__

    def __getitem__(self, key: str) -> Any:
        return SimpleNamespace(value=self.__dict__[key])


def _cfg(**overrides: Any) -> LdapConfig:
    base = LdapConfig(
        enabled=True,
        server="ldap.test",
        username_attr="userPrincipalName",
        user_search_filter="(userPrincipalName={username})",
    )
    return dataclasses.replace(base, **overrides)


def _login_and_sweep(monkeypatch, cfg: LdapConfig) -> tuple[bool, bool]:
    entry = _Entry(userPrincipalName=UPN, mail="jane.doe@mail.example", cn="Jane")
    monkeypatch.setattr(ldap_auth, "_search_ldap_user", lambda *a, **k: entry)
    monkeypatch.setattr(ldap_auth, "_get_user_groups", lambda *a, **k: [])
    monkeypatch.setattr(ldap_auth, "_check_group_access", lambda *a, **k: True)
    monkeypatch.setattr(ldap_auth, "_is_ad_account_disabled", lambda *a, **k: False)

    attrs = _extract_user_attributes(cfg, entry, "jane")
    assert attrs is not None
    assert attrs["username"] == UPN
    login_admin = _is_ldap_admin(cfg, attrs["username"], attrs["groups"])

    probe = probe_ldap_user(cfg, SimpleNamespace(), attrs["username"], attrs["email"])
    assert probe.status == DIRECTORY_PRESENT
    return login_admin, probe.is_admin


@pytest.mark.parametrize("listed", [UPN, "  JANE@Corp.Example ", f"bob, {UPN}"])
def test_an_admin_whose_username_has_an_at_sign_stays_admin_in_the_sweep(monkeypatch, listed):
    assert _login_and_sweep(monkeypatch, _cfg(admin_users=listed)) == (True, True)


def test_the_uid_before_the_at_sign_is_not_an_admin_in_either_path(monkeypatch):
    assert _login_and_sweep(monkeypatch, _cfg(admin_users="jane")) == (False, False)


def test_an_unlisted_user_is_not_an_admin_in_either_path(monkeypatch):
    assert _login_and_sweep(monkeypatch, _cfg(admin_users="bob@corp.example")) == (False, False)
