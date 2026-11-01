"""``ldap_authenticate`` end to end against a scripted directory: who is bound, with what, and when.

The helpers each have their own tests, but nothing pinned the ORDER and the refusals of the
login itself: that an empty password never reaches the directory (an empty-password simple
bind is an anonymous bind, which many servers accept), that the password is verified by
binding as the DN the search found (not the typed name, not the service account), that a
required-group refusal happens before the password is ever tried, and that both connections
are unbound afterwards. Each test below was watched failing against a deliberate break of
the line it pins (``~/ot-cleanup-work/b11/ldap``).
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

import pytest
from ldap3.core.exceptions import LDAPBindError

from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import ldap_authenticate
from tests.fixtures.ldap_directory_fake import SEARCH_BASE
from tests.fixtures.ldap_directory_fake import SERVICE_CREDENTIAL
from tests.fixtures.ldap_directory_fake import SERVICE_DN
from tests.fixtures.ldap_directory_fake import FakeEntry
from tests.fixtures.ldap_directory_fake import install

ALICE_DN = "uid=alice,dc=test"
ALICE_PW = "alice-pw"
STAFF = "CN=Staff,DC=test"


def _cfg(**overrides: Any) -> LdapConfig:
    base = LdapConfig(
        enabled=True,
        server="ldap.test",
        use_ssl=True,
        bind_dn=SERVICE_DN,
        bind_password=SERVICE_CREDENTIAL,
        search_base=SEARCH_BASE,
        user_search_filter="(uid={username})",
    )
    return dataclasses.replace(base, **overrides)


def _alice(**attrs) -> FakeEntry:
    values = {"uid": "alice", "mail": "alice@test.example", "cn": "Alice", "memberOf": [STAFF]}
    values.update(attrs)
    return FakeEntry(ALICE_DN, values)


@pytest.fixture
def directory(monkeypatch):
    d = install(monkeypatch)
    d.add_user(_alice(), ALICE_PW)
    return d


def _use(monkeypatch, cfg: LdapConfig) -> None:
    monkeypatch.setattr(LdapConfig, "from_env", classmethod(lambda cls: cfg))


def test_valid_credentials_bind_as_the_found_dn_and_return_its_attributes(directory, monkeypatch):
    _use(monkeypatch, _cfg())

    result = ldap_authenticate("alice", ALICE_PW)

    assert result == {
        "username": "alice",
        "email": "alice@test.example",
        "full_name": "Alice",
        "is_admin": False,
        "groups": [STAFF],
    }
    assert directory.binds() == [(SERVICE_DN, SERVICE_CREDENTIAL), (ALICE_DN, ALICE_PW)]


def test_a_wrong_password_is_refused(directory, monkeypatch):
    _use(monkeypatch, _cfg())

    assert ldap_authenticate("alice", "not-the-password") is None
    assert directory.binds()[-1] == (ALICE_DN, "not-the-password")


def test_a_user_bind_that_raises_is_a_refusal_not_a_login(directory, monkeypatch):
    directory.bind_errors[ALICE_DN] = LDAPBindError("invalidCredentials")
    _use(monkeypatch, _cfg())

    assert ldap_authenticate("alice", ALICE_PW) is None


@pytest.mark.parametrize("username,password", [("alice", ""), ("", ALICE_PW)])
def test_empty_credentials_never_reach_the_directory(directory, monkeypatch, username, password):
    _use(monkeypatch, _cfg())

    assert ldap_authenticate(username, password) is None
    assert directory.calls == []


def test_ldap_disabled_contacts_nothing(directory, monkeypatch):
    _use(monkeypatch, _cfg(enabled=False))

    assert ldap_authenticate("alice", ALICE_PW) is None
    assert directory.calls == []


def test_a_failed_service_bind_stops_the_login_cleanly(directory, monkeypatch, caplog):
    _use(monkeypatch, _cfg(bind_password="wrong"))

    with caplog.at_level(logging.ERROR):
        assert ldap_authenticate("alice", ALICE_PW) is None

    assert directory.searches_made() == []
    assert "Unexpected error" not in caplog.text


def test_a_required_group_refusal_happens_before_the_password_is_tried(directory, monkeypatch):
    _use(monkeypatch, _cfg(user_groups="CN=Admins,DC=test"))

    assert ldap_authenticate("alice", ALICE_PW) is None
    assert directory.binds() == [(SERVICE_DN, SERVICE_CREDENTIAL)]


def test_a_member_of_a_required_group_logs_in(directory, monkeypatch):
    _use(monkeypatch, _cfg(user_groups=f"CN=Other,DC=test;{STAFF.lower()}"))

    result = ldap_authenticate("alice", ALICE_PW)

    assert result is not None
    assert result["username"] == "alice"
    assert directory.binds()[-1] == (ALICE_DN, ALICE_PW)


def test_an_entry_without_a_valid_mail_is_refused_before_the_password(monkeypatch):
    directory = install(monkeypatch)
    directory.add_user(_alice(mail="not-an-address"), ALICE_PW)
    _use(monkeypatch, _cfg())

    assert ldap_authenticate("alice", ALICE_PW) is None
    assert directory.binds() == [(SERVICE_DN, SERVICE_CREDENTIAL)]


def test_the_typed_name_is_escaped_into_both_search_filters(directory, monkeypatch):
    _use(monkeypatch, _cfg())

    assert ldap_authenticate("x*)(uid=*@test.example", ALICE_PW) is None

    assert [s["filter"] for s in directory.searches_made()] == [
        "(uid=x\\2a\\29\\28uid=\\2a)",
        "(mail=x\\2a\\29\\28uid=\\2a@test.example)",
    ]


def test_both_connections_are_unbound_after_a_login(directory, monkeypatch):
    _use(monkeypatch, _cfg())

    assert ldap_authenticate("alice", ALICE_PW) is not None
    assert sorted(directory.unbinds()) == sorted([ALICE_DN, SERVICE_DN])


def test_ldaps_with_use_tls_set_does_not_also_negotiate_starttls(directory, monkeypatch):
    _use(monkeypatch, _cfg(use_ssl=True, use_tls=True))

    assert ldap_authenticate("alice", ALICE_PW) is not None
    assert [c for c in directory.calls if c[0] == "start_tls"] == []


def test_starttls_refused_means_no_bind_at_all(directory, monkeypatch):
    directory.start_tls_result = False
    _use(monkeypatch, _cfg(use_ssl=False, use_tls=True))

    assert ldap_authenticate("alice", ALICE_PW) is None
    assert directory.binds() == []
