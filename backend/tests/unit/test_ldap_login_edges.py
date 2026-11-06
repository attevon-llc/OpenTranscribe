"""The LDAP login and probe paths at their edges: odd entries, odd failures, and what is logged.

``test_ldap_authenticate_flow.py`` pins the happy path and the ordinary refusals. This module
pins what it leaves open: the server object's construction arguments, a non-LDAP exception
mid-login, entries that lack the username or name attribute (and what ``admin_users`` is then
compared against), the shapes a group attribute can take, a read timeout during the search or
during StartTLS negotiation, and that no credential ever reaches a log line.
"""

from __future__ import annotations

import dataclasses
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from ldap3.core.exceptions import LDAPException
from ldap3.core.exceptions import LDAPSocketReceiveError

from app.auth import ldap_directory
from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import ldap_authenticate
from app.auth.ldap_directory import _get_user_groups
from app.auth.ldap_probe import DIRECTORY_PRESENT
from app.auth.ldap_probe import LdapDirectoryUnavailableError
from app.auth.ldap_probe import probe_ldap_user
from tests.fixtures.ldap_directory_fake import SEARCH_BASE
from tests.fixtures.ldap_directory_fake import SERVICE_CREDENTIAL
from tests.fixtures.ldap_directory_fake import SERVICE_DN
from tests.fixtures.ldap_directory_fake import FakeDirectory
from tests.fixtures.ldap_directory_fake import FakeEntry
from tests.fixtures.ldap_directory_fake import bound_service_connection
from tests.fixtures.ldap_directory_fake import install

ALICE_DN = "uid=alice,dc=test"
ALICE_PW = "alice-pw-6f1c"
STAFF = "CN=Staff,DC=test"
_PLAINTEXT_SERVER = SimpleNamespace(ssl=False)  # an ldap:// server, so StartTLS must upgrade it


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


def _use(monkeypatch, cfg: LdapConfig) -> None:
    monkeypatch.setattr(LdapConfig, "from_env", classmethod(lambda cls: cfg))


def _alice(**attrs: Any) -> FakeEntry:
    values: dict[str, Any] = {
        "uid": "alice",
        "mail": "alice@test.example",
        "cn": "Alice",
        "memberOf": [STAFF],
    }
    values.update(attrs)
    return FakeEntry(ALICE_DN, {k: v for k, v in values.items() if v is not _ABSENT})


_ABSENT = object()


def _directory(monkeypatch, entry: FakeEntry | None = None) -> FakeDirectory:
    d = install(monkeypatch)
    d.searches[(SEARCH_BASE, "(uid=alice)")] = [entry or _alice()]
    d.passwords[ALICE_DN] = ALICE_PW
    return d


class TestServerObject:
    @pytest.mark.parametrize("use_ssl,use_tls,port", [(True, False, 636), (False, True, 389)])
    def test_the_host_is_positional_and_port_ssl_and_timeout_are_keywords(
        self, monkeypatch, use_ssl, use_tls, port
    ):
        d = _directory(monkeypatch)
        cfg = _cfg(server="dir.test", port=port, use_ssl=use_ssl, use_tls=use_tls, timeout=9)
        _use(monkeypatch, cfg)

        assert ldap_authenticate("alice", ALICE_PW) is not None

        ((_, host, kwargs),) = [c for c in d.calls if c[0] == "server"]
        assert host == "dir.test"
        assert kwargs["port"] == port
        assert kwargs["use_ssl"] is use_ssl
        assert kwargs["connect_timeout"] == 9

    def test_one_server_object_serves_both_binds_of_a_login(self, monkeypatch):
        d = _directory(monkeypatch)
        _use(monkeypatch, _cfg())

        assert ldap_authenticate("alice", ALICE_PW) is not None
        assert len([c for c in d.calls if c[0] == "server"]) == 1


class TestUnexpectedFailures:
    def test_a_non_ldap_exception_during_login_is_a_refusal(self, monkeypatch, caplog):
        d = _directory(monkeypatch)
        d.server_error = RuntimeError("socket library exploded")
        _use(monkeypatch, _cfg())

        with caplog.at_level(logging.ERROR):
            assert ldap_authenticate("alice", ALICE_PW) is None

        assert "RuntimeError" in caplog.text
        assert d.binds() == []

    def test_an_ldap_exception_during_the_search_is_a_refusal(self, monkeypatch, caplog):
        d = _directory(monkeypatch)
        d.searches[(SEARCH_BASE, "(uid=alice)")] = LDAPException("operations error")
        _use(monkeypatch, _cfg())

        with caplog.at_level(logging.ERROR):
            assert ldap_authenticate("alice", ALICE_PW) is None

        assert "LDAPException" in caplog.text
        assert d.binds() == [(SERVICE_DN, SERVICE_CREDENTIAL)]
        assert d.unbinds() == [SERVICE_DN]


class TestAttributeFallbacks:
    def test_an_entry_without_the_username_attribute_uses_the_typed_name(self, monkeypatch):
        _directory(monkeypatch, _alice(uid=_ABSENT))
        _use(monkeypatch, _cfg())

        result = ldap_authenticate("alice@test.example", ALICE_PW)

        assert result is not None
        assert result["username"] == "alice"

    def test_a_none_username_value_uses_the_typed_name(self, monkeypatch):
        _directory(monkeypatch, _alice(uid=None))
        _use(monkeypatch, _cfg())

        result = ldap_authenticate("alice", ALICE_PW)

        assert result is not None and result["username"] == "alice"

    @pytest.mark.parametrize("cn", [_ABSENT, None])
    def test_a_missing_name_is_an_empty_full_name(self, monkeypatch, cn):
        _directory(monkeypatch, _alice(cn=cn))
        _use(monkeypatch, _cfg())

        result = ldap_authenticate("alice", ALICE_PW)

        assert result is not None and result["full_name"] == ""

    @pytest.mark.parametrize("mail", [_ABSENT, None, "", "not-an-address"])
    def test_a_missing_or_malformed_email_refuses_before_the_password_bind(
        self, monkeypatch, caplog, mail
    ):
        d = _directory(monkeypatch, _alice(mail=mail))
        _use(monkeypatch, _cfg())

        with caplog.at_level(logging.WARNING):
            assert ldap_authenticate("alice", ALICE_PW) is None

        assert d.binds() == [(SERVICE_DN, SERVICE_CREDENTIAL)]
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_admin_users_matches_the_fallback_name_at_login(self, monkeypatch):
        _directory(monkeypatch, _alice(uid=_ABSENT))
        _use(monkeypatch, _cfg(admin_users="alice"))

        result = ldap_authenticate("alice@test.example", ALICE_PW)

        assert result is not None and result["is_admin"] is True


class TestProbeUsernameFallback:
    """The sweep's fallback is the stored ``ldap_uid`` whole, not its part before ``@``."""

    def _probe(self, cfg: LdapConfig, entry: FakeEntry, ldap_uid: str):
        d = FakeDirectory()
        d.searches[(SEARCH_BASE, "(uid=alice)")] = [entry]
        return probe_ldap_user(cfg, bound_service_connection(d), ldap_uid)

    @pytest.mark.parametrize("uid", [_ABSENT, None])
    def test_admin_users_listing_the_stored_uid_is_admin(self, uid):
        probe = self._probe(_cfg(admin_users="alice@corp"), _alice(uid=uid), "alice@corp")

        assert probe.status == DIRECTORY_PRESENT and probe.is_admin is True

    def test_the_part_before_the_at_sign_is_not_the_fallback(self):
        probe = self._probe(_cfg(admin_users="alice"), _alice(uid=_ABSENT), "alice@corp")

        assert probe.status == DIRECTORY_PRESENT and probe.is_admin is False

    def test_the_entry_attribute_wins_over_the_stored_uid(self):
        """With the attribute present, the stored uid plays no part in the comparison."""
        probe = self._probe(_cfg(admin_users="alice@corp"), _alice(uid="alice"), "alice@corp")

        assert probe.is_admin is False


class TestGroupAttributeShapes:
    def test_a_list_value_becomes_a_list_of_strings(self):
        entry = FakeEntry(ALICE_DN, {"memberOf": [STAFF, 42]})
        assert _get_user_groups(_cfg(), entry) == [STAFF, "42"]

    def test_a_scalar_value_becomes_a_one_element_list(self):
        entry = FakeEntry(ALICE_DN, {"memberOf": STAFF})
        assert _get_user_groups(_cfg(), entry) == [STAFF]

    @pytest.mark.parametrize("attrs", [{"memberOf": None}, {}, {"memberOf": []}])
    def test_no_value_is_no_groups(self, attrs):
        assert _get_user_groups(_cfg(), FakeEntry(ALICE_DN, attrs)) == []

    def test_an_empty_group_attribute_setting_reads_nothing(self):
        entry = FakeEntry(ALICE_DN, {"memberOf": [STAFF], "": [STAFF]})
        assert _get_user_groups(_cfg(group_attr=""), entry) == []

    def test_the_configured_attribute_is_the_one_read(self):
        entry = FakeEntry(ALICE_DN, {"memberOf": [STAFF], "isMemberOf": ["CN=Other,DC=test"]})
        assert _get_user_groups(_cfg(group_attr="isMemberOf"), entry) == ["CN=Other,DC=test"]

    def test_login_returns_a_scalar_group_as_a_list(self, monkeypatch):
        _directory(monkeypatch, _alice(memberOf=STAFF))
        _use(monkeypatch, _cfg())

        result = ldap_authenticate("alice", ALICE_PW)

        assert result is not None and result["groups"] == [STAFF]


class TestReadTimeouts:
    def test_a_search_timeout_at_login_is_a_logged_refusal(self, monkeypatch, caplog):
        d = _directory(monkeypatch)
        d.searches[(SEARCH_BASE, "(uid=alice)")] = LDAPSocketReceiveError("timed out")
        _use(monkeypatch, _cfg())

        with caplog.at_level(logging.ERROR):
            assert ldap_authenticate("alice", ALICE_PW) is None

        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("LDAPSocketReceiveError" in m for m in errors)
        assert d.unbinds() == [SERVICE_DN]

    def test_a_search_timeout_in_the_probe_is_unavailable(self):
        d = FakeDirectory()
        d.searches[(SEARCH_BASE, "(uid=alice)")] = LDAPSocketReceiveError("timed out")

        with pytest.raises(LdapDirectoryUnavailableError) as raised:
            probe_ldap_user(_cfg(), bound_service_connection(d), "alice")

        assert str(raised.value) == "LDAP search failed (LDAPSocketReceiveError)"

    @pytest.mark.parametrize("stalls_in", ["open", "start_tls"])
    def test_a_starttls_handshake_timeout_is_refused_and_never_binds(
        self, monkeypatch, caplog, stalls_in
    ):
        made: list[_StallingConnection] = []

        def _connection(server: Any, **options: Any) -> _StallingConnection:
            conn = _StallingConnection(stalls_in)
            made.append(conn)
            return conn

        monkeypatch.setattr(ldap_directory, "Connection", _connection)
        cfg = _cfg(use_ssl=False, use_tls=True, timeout=4)

        with caplog.at_level(logging.ERROR, logger="app.auth.ldap_directory"):
            assert ldap_directory._connect_and_bind(cfg, _PLAINTEXT_SERVER, "cn=a", "pw") is None

        assert "did not answer within 4 s" in caplog.text
        (conn,) = made
        expected = ["open"] if stalls_in == "open" else ["open", "start_tls"]
        assert conn.calls[: len(expected)] == expected
        assert "bind" not in conn.calls


class _StallingConnection:
    """A StartTLS connection whose ``open`` or ``start_tls`` outlasts the read timeout."""

    def __init__(self, stalls_in: str) -> None:
        self.stalls_in = stalls_in
        self.calls: list[str] = []
        self.bound = False

    def _step(self, name: str) -> bool:
        self.calls.append(name)
        if name == self.stalls_in:
            raise LDAPSocketReceiveError("timed out")
        return True

    def open(self) -> bool:
        return self._step("open")

    def start_tls(self) -> bool:
        return self._step("start_tls")

    def bind(self) -> bool:
        return self._step("bind")

    def unbind(self) -> bool:
        self.calls.append("unbind")
        return True


def _no_change(d: FakeDirectory) -> None:
    pass


def _refuse_service(d: FakeDirectory) -> None:
    d.passwords[SERVICE_DN] = "a-different-credential"


def _refuse_user(d: FakeDirectory) -> None:
    d.passwords[ALICE_DN] = "the-real-one"


def _refuse_starttls(d: FakeDirectory) -> None:
    d.start_tls_result = False


def _fail_search(d: FakeDirectory) -> None:
    d.searches[(SEARCH_BASE, "(uid=alice)")] = LDAPException("operations error")


def _fail_unexpectedly(d: FakeDirectory) -> None:
    d.server_error = RuntimeError("boom")


#: scenario -> (directory change, config overrides, whether the login succeeds)
LOG_SCENARIOS: dict[str, tuple[Any, dict[str, Any], bool]] = {
    "success": (_no_change, {}, True),
    "service bind refused": (_refuse_service, {}, False),
    "wrong user password": (_refuse_user, {}, False),
    "starttls refused": (_refuse_starttls, {"use_ssl": False, "use_tls": True}, False),
    "search error": (_fail_search, {}, False),
    "unexpected error": (_fail_unexpectedly, {}, False),
    "not entitled": (_no_change, {"user_groups": "CN=Ops,DC=test"}, False),
}


@pytest.mark.parametrize("scenario", sorted(LOG_SCENARIOS))
def test_no_credential_reaches_a_log_record(monkeypatch, caplog, scenario):
    """Neither the service password nor the user's password may appear in any record."""
    change, overrides, succeeds = LOG_SCENARIOS[scenario]
    change(_directory(monkeypatch))
    _use(monkeypatch, _cfg(**overrides))

    with caplog.at_level(logging.DEBUG):
        result = ldap_authenticate("alice", ALICE_PW)

    assert (result is not None) is succeeds
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert caplog.records, "nothing was logged, so this proves nothing"
    assert SERVICE_CREDENTIAL not in text
    assert ALICE_PW not in text
