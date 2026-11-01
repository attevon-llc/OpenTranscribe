"""Group entitlement, admin signal and recursive membership, against a scripted directory.

``_check_group_access`` and ``_is_ldap_admin`` decide who may log in and who is an admin;
the recursive branch asks the directory with ``LDAP_MATCHING_RULE_IN_CHAIN``
(``1.2.840.113556.1.4.1941``). Pinned here: refusals stay refusals (recursive enabled or not),
the recursive lookup only runs when ``ldap_recursive_groups`` is on, a directory error on one
group never counts as membership, the chain filter is escaped and BASE-scoped, and the search
retry for servers without ``memberOf`` only fires for that error. Each test was watched
failing against a deliberate break of the line it pins (``~/ot-cleanup-work/b11/ldap``).
"""

from __future__ import annotations

import pytest
from ldap3.core.exceptions import LDAPException

from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import _is_ldap_admin
from app.auth.ldap_directory import _check_group_access
from app.auth.ldap_directory import _search_ldap_user
from app.auth.ldap_directory import _search_recursive_group_membership
from tests.fixtures.ldap_directory_fake import SEARCH_BASE
from tests.fixtures.ldap_directory_fake import FakeDirectory
from tests.fixtures.ldap_directory_fake import FakeEntry
from tests.fixtures.ldap_directory_fake import bound_service_connection

OID = "1.2.840.113556.1.4.1941"
USER_DN = "uid=dave,dc=test"
NESTED = "CN=Nested,DC=test"
OTHER = "CN=Other,DC=test"


def _chain_filter(group_dn: str, user_dn: str) -> str:
    return f"(&(distinguishedName={group_dn})(member:{OID}:={user_dn}))"


def _directory_with_nested_member(group_dn: str = NESTED, user_dn: str = USER_DN) -> FakeDirectory:
    """``user_dn`` is a member of ``group_dn`` only through nesting (not in ``memberOf``)."""
    group = FakeEntry(group_dn, {"distinguishedName": group_dn})
    d = FakeDirectory()
    d.searches[(group_dn, "(objectClass=*)")] = [group]
    d.searches[(group_dn, _chain_filter(group_dn, user_dn))] = [group]
    return d


class TestCheckGroupAccess:
    def test_no_required_groups_admits_everyone_without_asking_the_directory(self):
        d = FakeDirectory()
        conn = bound_service_connection(d)

        assert _check_group_access(LdapConfig(), conn, USER_DN, [], "dave") is True
        assert d.calls == []

    def test_a_direct_member_is_admitted_without_a_recursive_lookup(self):
        d = _directory_with_nested_member()
        conn = bound_service_connection(d)
        cfg = LdapConfig(user_groups=OTHER, recursive_groups=True)

        assert _check_group_access(cfg, conn, USER_DN, [OTHER], "dave") is True
        assert d.calls == []

    def test_a_non_member_is_refused_when_recursion_is_off(self):
        d = _directory_with_nested_member()
        conn = bound_service_connection(d)

        assert (
            _check_group_access(LdapConfig(user_groups=NESTED), conn, USER_DN, [], "dave") is False
        )
        assert d.calls == []

    def test_a_nested_member_is_admitted_when_recursion_is_on(self):
        d = _directory_with_nested_member()
        conn = bound_service_connection(d)
        cfg = LdapConfig(user_groups=NESTED, recursive_groups=True)

        assert _check_group_access(cfg, conn, USER_DN, [], "dave") is True

    def test_a_non_member_is_refused_when_recursion_is_on(self):
        d = _directory_with_nested_member(user_dn="uid=someone-else,dc=test")
        conn = bound_service_connection(d)
        cfg = LdapConfig(user_groups=NESTED, recursive_groups=True)

        assert _check_group_access(cfg, conn, USER_DN, [], "dave") is False


class TestRecursiveMembership:
    def test_the_chain_query_is_escaped_and_base_scoped(self):
        group_dn = "CN=Team (A)*,DC=test"
        user_dn = "uid=a*b(c),dc=test"
        escaped = _chain_filter("CN=Team \\28A\\29\\2a,DC=test", "uid=a\\2ab\\28c\\29,dc=test")
        d = FakeDirectory()
        d.searches[(group_dn, "(objectClass=*)")] = [FakeEntry(group_dn, {})]
        d.searches[(group_dn, escaped)] = [FakeEntry(group_dn, {})]
        conn = bound_service_connection(d)

        assert _search_recursive_group_membership(conn, user_dn, [group_dn]) is True
        chain = d.searches_made()[1]
        assert chain["filter"] == escaped
        assert chain["search_scope"] == "BASE"

    def test_an_existing_group_without_the_user_is_not_membership(self):
        d = _directory_with_nested_member(user_dn="uid=someone-else,dc=test")
        conn = bound_service_connection(d)

        assert _search_recursive_group_membership(conn, USER_DN, [NESTED]) is False

    def test_a_directory_error_on_one_group_is_not_membership(self):
        d = FakeDirectory()
        d.searches[("CN=Broken,DC=test", "(objectClass=*)")] = LDAPException("unwilling")
        conn = bound_service_connection(d)

        assert _search_recursive_group_membership(conn, USER_DN, ["CN=Broken,DC=test"]) is False

    def test_a_directory_error_on_one_group_still_checks_the_next(self):
        d = _directory_with_nested_member()
        d.searches[("CN=Broken,DC=test", "(objectClass=*)")] = LDAPException("unwilling")
        conn = bound_service_connection(d)

        assert (
            _search_recursive_group_membership(conn, USER_DN, ["CN=Broken,DC=test", NESTED]) is True
        )


class TestAdminSignal:
    def test_admin_users_is_an_exact_name_match_not_a_substring(self):
        assert _is_ldap_admin(LdapConfig(admin_users="alice,bob"), "ali", []) is False
        assert _is_ldap_admin(LdapConfig(admin_users="alice, Bob "), "bob", []) is True

    def test_nested_admin_group_needs_recursion_enabled(self):
        d = _directory_with_nested_member()
        conn = bound_service_connection(d)

        cfg = LdapConfig(admin_groups=NESTED, recursive_groups=False)
        assert _is_ldap_admin(cfg, "dave", [], bind_conn=conn, user_dn=USER_DN) is False
        assert d.calls == []

    def test_nested_admin_group_counts_when_recursion_is_enabled(self):
        d = _directory_with_nested_member()
        conn = bound_service_connection(d)

        cfg = LdapConfig(admin_groups=NESTED, recursive_groups=True)
        assert _is_ldap_admin(cfg, "dave", [], bind_conn=conn, user_dn=USER_DN) is True


class TestSearchRetry:
    def _cfg(self) -> LdapConfig:
        return LdapConfig(search_base=SEARCH_BASE, user_search_filter="(uid={username})")

    def test_a_server_without_memberof_is_searched_again_with_the_base_attributes(self):
        entry = FakeEntry("uid=erin,dc=test", {"uid": "erin"})
        d = FakeDirectory(rejected_attribute="memberOf")
        d.searches[(SEARCH_BASE, "(uid=erin)")] = [entry]
        conn = bound_service_connection(d)

        found = _search_ldap_user(
            self._cfg(), conn, "erin", "erin", extra_attributes=["userAccountControl"]
        )

        assert found is entry
        assert [s["attributes"] for s in d.searches_made()] == [
            ["uid", "mail", "cn", "memberOf", "userAccountControl"],
            ["uid", "mail", "cn"],
        ]

    def test_any_other_directory_error_propagates_instead_of_being_retried(self):
        d = FakeDirectory()
        d.searches[(SEARCH_BASE, "(uid=erin)")] = LDAPException("server busy")
        conn = bound_service_connection(d)

        with pytest.raises(LDAPException, match="server busy"):
            _search_ldap_user(self._cfg(), conn, "erin", "erin")
        assert len(d.searches_made()) == 1
