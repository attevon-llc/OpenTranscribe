"""The directory-sync probes against a scripted directory: what they report and how they fail.

``probe_ldap_user``'s answer is what the periodic sweep deprovisions on, so a wrong
``absent`` / ``disabled`` disables a live account and a wrong ``present`` keeps an offboarded
one. Pinned here, through the real search path rather than a patched ``_search_ldap_user``:
the AD ``userAccountControl`` bit is requested and only a positive ACCOUNTDISABLE bit counts,
the stored email is the fallback lookup, the admin signal and groups travel with ``present``,
and every failure to ask raises a scrubbed ``LdapDirectoryUnavailableError`` (never a status,
never the directory's own message). Each test was watched failing against a deliberate break
of the line it pins (``~/ot-cleanup-work/b11/ldap``).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from ldap3.core.exceptions import LDAPException

from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_probe import DIRECTORY_ABSENT
from app.auth.ldap_probe import DIRECTORY_DISABLED
from app.auth.ldap_probe import DIRECTORY_PRESENT
from app.auth.ldap_probe import LdapDirectoryUnavailableError
from app.auth.ldap_probe import LdapProbe
from app.auth.ldap_probe import ldap_directory_session
from app.auth.ldap_probe import probe_ldap_user
from tests.fixtures.ldap_directory_fake import SEARCH_BASE
from tests.fixtures.ldap_directory_fake import SERVICE_CREDENTIAL
from tests.fixtures.ldap_directory_fake import SERVICE_DN
from tests.fixtures.ldap_directory_fake import FakeDirectory
from tests.fixtures.ldap_directory_fake import FakeEntry
from tests.fixtures.ldap_directory_fake import bound_service_connection
from tests.fixtures.ldap_directory_fake import install

STAFF = "CN=Staff,DC=test"
SECRET = "ldaps://dc01.corp.internal:636 bind cn=svc,ou=svc"


def _cfg(**overrides: Any) -> LdapConfig:
    base = LdapConfig(
        enabled=True,
        server="ldap.test",
        bind_dn=SERVICE_DN,
        bind_password=SERVICE_CREDENTIAL,
        search_base=SEARCH_BASE,
        user_search_filter="(uid={username})",
    )
    return dataclasses.replace(base, **overrides)


def _directory_with(**attrs) -> FakeDirectory:
    values = {"uid": "alice", "mail": "alice@test.example", "cn": "Alice", "memberOf": [STAFF]}
    values.update(attrs)
    d = FakeDirectory()
    d.searches[(SEARCH_BASE, "(uid=alice)")] = [FakeEntry("uid=alice,dc=test", values)]
    return d


def _probe(d: FakeDirectory, cfg: LdapConfig | None = None, uid: str = "alice", email: str = ""):
    return probe_ldap_user(cfg or _cfg(), bound_service_connection(d), uid, email)


class TestAccountState:
    def test_the_search_asks_for_user_account_control(self):
        d = _directory_with()
        _probe(d)
        assert d.searches_made()[0]["attributes"] == [
            "uid",
            "mail",
            "cn",
            "memberOf",
            "userAccountControl",
        ]

    @pytest.mark.parametrize("value", ["2", 2, "514", 0x10202])
    def test_the_accountdisable_bit_reports_disabled(self, value):
        assert _probe(_directory_with(userAccountControl=value)) == LdapProbe(DIRECTORY_DISABLED)

    @pytest.mark.parametrize("value", ["512", 512, 0, None, "garbage"])
    def test_anything_else_is_not_disabled(self, value):
        assert _probe(_directory_with(userAccountControl=value)).status == DIRECTORY_PRESENT

    def test_a_present_account_carries_its_groups(self):
        assert _probe(_directory_with()) == LdapProbe(DIRECTORY_PRESENT, (STAFF,), False)

    def test_the_stored_email_is_the_fallback_lookup(self):
        d = _directory_with()
        d.searches[(SEARCH_BASE, "(mail=alice@test.example)")] = d.searches.pop(
            (SEARCH_BASE, "(uid=alice)")
        )

        assert _probe(d, uid="alice", email="alice@test.example").status == DIRECTORY_PRESENT
        assert _probe(FakeDirectory(), uid="alice", email="alice@test.example") == LdapProbe(
            DIRECTORY_ABSENT
        )


class TestAdminSignal:
    def test_admin_users_matches_the_uid_before_the_at_sign(self):
        probe = _probe(_directory_with(), _cfg(admin_users="alice"), uid="alice@corp")

        assert probe == LdapProbe(DIRECTORY_PRESENT, (STAFF,), True)

    def test_admin_groups_membership_is_reported(self):
        assert _probe(_directory_with(), _cfg(admin_groups=STAFF)).is_admin is True


class TestFailuresAreUnavailableAndScrubbed:
    def test_a_search_error_is_unavailable_with_only_the_error_type(self):
        d = FakeDirectory()
        d.searches[(SEARCH_BASE, "(uid=alice)")] = LDAPException(SECRET)

        with pytest.raises(LdapDirectoryUnavailableError) as raised:
            _probe(d)
        assert str(raised.value) == "LDAP search failed (LDAPException)"

    def test_a_group_check_error_is_unavailable_not_unentitled(self, monkeypatch):
        def _raise(*_a, **_k):
            raise LDAPException(SECRET)

        monkeypatch.setattr("app.auth.ldap_probe._check_group_access", _raise)

        with pytest.raises(LdapDirectoryUnavailableError) as raised:
            _probe(_directory_with())
        assert str(raised.value) == "LDAP group check failed (LDAPException)"

    def test_an_unexpected_connection_error_is_unavailable_with_only_the_error_type(
        self, monkeypatch
    ):
        install(monkeypatch, FakeDirectory(server_error=RuntimeError(SECRET)))

        with pytest.raises(LdapDirectoryUnavailableError) as raised:
            with ldap_directory_session(_cfg()):
                pytest.fail("the session must not yield")
        assert str(raised.value) == "LDAP connection failed (RuntimeError)"

    def test_a_refused_service_bind_is_unavailable(self, monkeypatch):
        install(monkeypatch)

        with pytest.raises(LdapDirectoryUnavailableError, match="returned no connection"):
            with ldap_directory_session(_cfg(bind_password="wrong")):
                pytest.fail("the session must not yield")


class TestSessionLifecycle:
    def test_the_service_connection_is_unbound_after_use(self, monkeypatch):
        d = install(monkeypatch)

        with ldap_directory_session(_cfg()) as conn:
            assert conn.bound is True
        assert d.unbinds() == [SERVICE_DN]

    def test_the_service_connection_is_unbound_when_the_body_raises(self, monkeypatch):
        d = install(monkeypatch)

        with pytest.raises(ValueError, match="body failed"):
            with ldap_directory_session(_cfg()):
                raise ValueError("body failed")
        assert d.unbinds() == [SERVICE_DN]
