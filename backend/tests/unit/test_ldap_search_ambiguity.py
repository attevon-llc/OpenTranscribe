"""Regression test: an ambiguous LDAP search must not silently bind entries[0].

``_search_ldap_user`` (``app/auth/ldap_auth.py``) searches by username and then, on
a miss, by email. If either search returns MORE THAN ONE entry — an over-broad
filter, or genuine directory duplicates — binding as ``entries[0]`` authenticates
the caller as whichever account the directory happened to list first. That is an
authentication-ambiguity bug, and a deliberate one if an attacker with any
directory write access can create a colliding entry. The fix fails closed: an
ambiguous match is treated as "not found" and logged as an error.
"""

from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import _search_ldap_user

CFG = LdapConfig(
    enabled=True,
    server="ldap.example.com",
    search_base="DC=example,DC=com",
    user_search_filter="(uid={username})",
)


class FakeEntry:
    def __init__(self, dn):
        self.entry_dn = dn

    def __contains__(self, key):
        return False

    def __getitem__(self, key):
        raise KeyError(key)


class FakeBindConn:
    """Minimal stand-in for an ldap3 ``Connection`` used only for ``.search``."""

    def __init__(self, entries):
        self._entries = entries
        self.entries = []

    def search(self, search_base, search_filter, attributes):
        self.entries = self._entries


class TestSearchLdapUserAmbiguity:
    def test_single_match_returns_that_entry(self):
        entry = FakeEntry("CN=alice,DC=example,DC=com")
        conn = FakeBindConn([entry])
        result = _search_ldap_user(CFG, conn, "alice", "alice")
        assert result is entry

    def test_multiple_matches_refuses_rather_than_picking_first(self):
        """The core bug: entries[0] must never be an implicit choice."""
        first = FakeEntry("CN=alice,OU=Sales,DC=example,DC=com")
        second = FakeEntry("CN=alice,OU=Engineering,DC=example,DC=com")
        conn = FakeBindConn([first, second])
        result = _search_ldap_user(CFG, conn, "alice", "alice")
        assert result is None

    def test_no_matches_returns_none(self):
        conn = FakeBindConn([])
        result = _search_ldap_user(CFG, conn, "ghost", "ghost")
        assert result is None

    def test_ambiguous_username_match_does_not_fall_back_to_email_search(self):
        """An ambiguous primary match fails closed instead of trying another path
        that might resolve to a different (also plausible) account."""
        first = FakeEntry("CN=alice,OU=Sales,DC=example,DC=com")
        second = FakeEntry("CN=alice,OU=Engineering,DC=example,DC=com")
        conn = FakeBindConn([first, second])
        result = _search_ldap_user(CFG, conn, "alice@example.com", "alice")
        assert result is None
